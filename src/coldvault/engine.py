"""Création, copie, vérification et générations additives. Sans dépendances externes."""

import hashlib
import os
import shutil
import stat
import uuid
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path

from . import io
from .control import Control
from .errors import Cancelled, ColdVaultError, Conflict, SourceChanged, UnsafePath
from .io import Tree, hash_file, media_lock, signature
from .manifest import (
    FORMAT,
    Entry,
    Manifest,
    check_namespace,
    json_bytes,
    path_key,
    portable_path,
    strict_json,
    utc_now,
)

Sources = Mapping[str, Path | str]
MARKER = ".coldvault/media.json"


def load_manifest(path: Path | str) -> Manifest:
    path = Path(path).absolute()
    with Tree(path.parent) as tree:
        return Manifest.from_bytes(tree.read(path.name))


def export_new(path: Path | str, content: bytes) -> None:
    """Export exclusif ; le dossier parent doit déjà exister."""
    path = Path(path).absolute()
    with Tree(path.parent) as tree:
        tree.write_new(path.name, content)


def ensure_export_outside_sources(output: Path | str, sources: Sources) -> None:
    destination = Path(output).absolute().parent.resolve(strict=True) / Path(output).name
    if any(destination.is_relative_to(Path(source).resolve()) for source in sources.values()):
        raise UnsafePath("Exporter le manifeste ou rapport en dehors des dossiers sources")


def _source_trees(sources: Sources, stack: ExitStack) -> dict[str, Tree]:
    check_namespace(list(sources))
    for alias in sources:
        if len(portable_path(alias)) != 1:
            raise UnsafePath("Chaque source doit avoir un alias simple")
    return {alias: stack.enter_context(Tree(root)) for alias, root in sources.items()}


def _scan(trees: dict[str, Tree], control: Control) -> list[tuple[str, Tree, str, int]]:
    found = []
    names: dict[str, str] = {}
    for alias, tree in trees.items():
        for relative, info in tree.walk():
            control.checkpoint()
            logical = f"{alias}/{relative}"
            key = path_key(logical)
            if names.setdefault(key, logical) != logical:
                raise UnsafePath(f"Conflit de casse/Unicode : {logical}")
            if stat.S_ISREG(info.st_mode):
                found.append((logical, tree, relative, info.st_size))
    check_namespace([item[0] for item in found])
    return sorted(found, key=lambda item: item[0])


def create_manifest(
    sources: Sources, name: str, *, previous: Manifest | None = None, control: Control | None = None
) -> Manifest:
    """Une préparation ne valide aucun support. Toute source courante est relue intégralement."""
    control = control or Control()
    control.checkpoint()
    if previous:
        previous.validate()
        if not set(previous.roots).issubset(sources):
            raise ColdVaultError(
                "Toutes les racines antérieures sont requises, même devenues vides"
            )
    with ExitStack() as stack:
        trees = _source_trees(sources, stack)
        found = _scan(trees, control)
        total = sum(item[3] for item in found)
        done = 0
        generation = previous.generation + 1 if previous else 1
        entries = list(previous.files) if previous else []
        latest = {}
        for entry in entries:
            if (
                entry.logical_path not in latest
                or latest[entry.logical_path].revision < entry.revision
            ):
                latest[entry.logical_path] = entry
        for logical, tree, relative, size in found:
            digest, actual_size, mtime = hash_file(
                tree,
                relative,
                control,
                lambda n, p=logical, s=size, d=done: control.emit(
                    "empreintes", p, n, s, d + n, total
                ),
            )
            done += actual_size
            old = latest.get(logical)
            if old and (old.sha256, old.size) == (digest, actual_size):
                continue
            entries.append(
                Entry(
                    f"data/{generation:06d}/{logical}",
                    logical,
                    actual_size,
                    digest,
                    generation,
                    mtime,
                )
            )
        # Un ajout/disparition pendant l'analyse invalide la préparation.
        second = _scan(trees, control)
        if [(x[0], x[3]) for x in found] != [(x[0], x[3]) for x in second]:
            raise SourceChanged("L'inventaire des sources a changé pendant l'analyse")
    manifest = Manifest(
        FORMAT,
        previous.archive_id if previous else str(uuid.uuid4()),
        name,
        generation,
        utc_now(),
        tuple(sorted(sources)),
        tuple(sorted(entries, key=lambda e: e.path)),
        len(entries),
        sum(e.size for e in entries),
        previous.digest if previous else None,
    )
    manifest.validate()
    return manifest


def update_summary(previous: Manifest, candidate: Manifest) -> dict[str, list[str]]:
    if candidate.archive_id != previous.archive_id or candidate.parent_sha256 != previous.digest:
        raise ColdVaultError("Cette génération ne descend pas du manifeste fourni")
    old = {e.logical_path for e in previous.files}
    added = [e.logical_path for e in candidate.files if e.revision == candidate.generation]
    return {
        "new": sorted(set(added) - old),
        "modified": sorted(set(added) & old),
        "retained_history": sorted(e.path for e in previous.files),
    }


def source_comparison(manifest: Manifest, sources: Sources) -> dict[str, list[str]]:
    """Les nouveaux/modifiés sont dans update_summary ; signale absents et inchangés."""
    with ExitStack() as stack:
        trees = _source_trees(sources, stack)
        current = {item[0] for item in _scan(trees, Control())}
    latest = {}
    for entry in sorted(manifest.files, key=lambda e: e.revision):
        latest[entry.logical_path] = entry
    return {
        "deleted_from_sources": sorted(set(latest) - current),
        "unchanged": sorted(
            p for p in current if p in latest and latest[p].revision < manifest.generation
        ),
    }


@dataclass(frozen=True)
class Result:
    path: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class Report:
    report_id: str
    archive_id: str
    manifest_sha256: str
    generation: int
    media_id: str | None
    started_utc: str
    finished_utc: str
    context: str
    reconnection_evidence: str
    results: tuple[Result, ...]

    @property
    def ok(self) -> bool:
        return all(r.status == "verified" for r in self.results)

    def to_bytes(self) -> bytes:
        return json_bytes({**asdict(self), "ok": self.ok})


def _inspect(tree: Tree, entry: Entry, control: Control, on_chunk=None) -> Result:
    try:
        info = tree.stat(entry.path)
        if not stat.S_ISREG(info.st_mode):
            return Result(entry.path, "io_error", "Le chemin n'est pas un fichier régulier")
        if info.st_size != entry.size:
            return Result(entry.path, "size_mismatch", f"Attendu {entry.size}, lu {info.st_size}")
        digest, _, _ = hash_file(tree, entry.path, control, on_chunk)
        return Result(entry.path, "verified" if digest == entry.sha256 else "hash_mismatch")
    except FileNotFoundError:
        return Result(entry.path, "missing")
    except (OSError, ColdVaultError) as exc:
        if isinstance(exc, Cancelled):
            raise
        return Result(entry.path, "io_error", str(exc))


def read_media(tree: Tree, manifest: Manifest, expected_id: str | None = None) -> dict:
    value = strict_json(tree.read(MARKER))
    if not isinstance(value, dict) or set(value) != {
        "format_version",
        "media_id",
        "archive_id",
        "label",
        "created_utc",
    }:
        raise ColdVaultError("Identité du support invalide")
    try:
        if (
            value["format_version"] != 1
            or str(uuid.UUID(value["media_id"])) != value["media_id"]
            or value["archive_id"] != manifest.archive_id
        ):
            raise ValueError("Identité ou archive différente")
        if expected_id is not None and value["media_id"] != expected_id:
            raise ValueError("Ce support ne correspond pas à l'identifiant attendu")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ColdVaultError(str(exc)) from exc
    return value


def initialize_media(destination: Path | str, manifest: Manifest, label: str) -> dict:
    """Association explicite d'un dossier vide à une archive, sans formatage du disque."""
    manifest.validate()
    if not label.strip():
        raise ColdVaultError("Nom du support requis")
    with Tree(destination) as tree:
        # Refuse toute donnée préexistante. L'utilisateur choisit un dossier dédié vide.
        if list(tree.walk()):
            raise Conflict("L'initialisation exige un dossier vide")
        with media_lock(tree):
            value = {
                "format_version": 1,
                "media_id": str(uuid.uuid4()),
                "archive_id": manifest.archive_id,
                "label": label,
                "created_utc": utc_now(),
            }
            tree.write_new(MARKER, json_bytes(value))
            return value


def verify(
    manifest: Manifest,
    destination: Path | str | Tree,
    *,
    control: Control | None = None,
    context: str = "standalone",
    expected_media_id: str | None = None,
    user_declared_reconnected: bool = False,
) -> Report:
    manifest.validate()
    if context not in {"standalone", "immediate", "after_reconnection"}:
        raise ColdVaultError("Contexte de vérification inconnu")
    if context == "after_reconnection" and not user_declared_reconnected:
        raise ColdVaultError("La reconnexion doit être déclarée explicitement par l'utilisateur")
    control = control or Control()
    started = utc_now()
    results: list[Result] = []
    done = 0
    with ExitStack() as stack:
        tree = (
            destination if isinstance(destination, Tree) else stack.enter_context(Tree(destination))
        )
        # La vérification pure reste possible sur une copie manuelle sans marqueur.
        media_id = None
        if tree.exists(MARKER) or expected_media_id is not None:
            media_id = read_media(tree, manifest, expected_media_id)["media_id"]
        interrupted = False
        for entry in manifest.files:
            if interrupted:
                results.append(Result(entry.path, "not_verified", "Opération interrompue"))
                continue
            try:
                control.checkpoint()
                result = _inspect(
                    tree,
                    entry,
                    control,
                    lambda n, e=entry, d=done: control.emit(
                        "verification", e.path, n, e.size, d + n, manifest.total_bytes
                    ),
                )
                results.append(result)
                done += entry.size
            except Cancelled:
                interrupted = True
                results.append(Result(entry.path, "not_verified", "Opération interrompue"))
        if not interrupted:
            expected = {e.path for e in manifest.files}
            try:
                control.checkpoint()
                for path, info in tree.walk(skip_metadata=True):
                    control.checkpoint()
                    if stat.S_ISREG(info.st_mode) and path not in expected:
                        status = "temporary" if Path(path).name.startswith(".cv-") else "extra"
                        results.append(Result(path, status))
            except Cancelled:
                results.append(Result("<inventaire>", "not_verified", "Inventaire interrompu"))
            except (OSError, ColdVaultError) as exc:
                results.append(Result("<inventaire>", "io_error", str(exc)))
        if interrupted and not manifest.files:
            results.append(Result("<inventaire>", "not_verified"))
        try:
            tree.check_root()
        except (OSError, ColdVaultError) as exc:
            results.append(Result("<racine>", "io_error", str(exc)))
    return Report(
        str(uuid.uuid4()),
        manifest.archive_id,
        manifest.digest,
        manifest.generation,
        media_id,
        started,
        utc_now(),
        context,
        "user_declaration" if user_declared_reconnected else "none",
        tuple(results),
    )


def _assert_separate(trees: dict[str, Tree], destination: Tree) -> None:
    for tree in trees.values():
        if tree.root.is_relative_to(destination.root) or destination.root.is_relative_to(tree.root):
            raise UnsafePath("Sources et destination doivent être des arbres distincts")


def _check_lineage(tree: Tree, manifest: Manifest) -> None:
    prefix = ".coldvault/manifests"
    # Pas de génération divergente ni de retour silencieux vers une génération ancienne.
    if not tree.exists(prefix):
        if manifest.generation != 1:
            raise Conflict("Copier d'abord les générations précédentes sur ce nouveau support")
        return
    known = []
    for path, info in tree.walk():
        if path.startswith(prefix + "/") and stat.S_ISREG(info.st_mode) and path.endswith(".json"):
            old = Manifest.from_bytes(tree.read(path))
            if old.archive_id != manifest.archive_id:
                raise Conflict("Un manifeste appartient à une autre archive")
            known.append(old)
    same = [old for old in known if old.generation == manifest.generation]
    if any(old.digest != manifest.digest for old in same):
        raise Conflict("Une autre génération porte déjà ce numéro")
    if any(old.generation > manifest.generation for old in known):
        raise Conflict("Le support possède une génération plus récente")
    if manifest.generation > 1:
        parent = [old for old in known if old.generation == manifest.generation - 1]
        if not any(old.digest == manifest.parent_sha256 for old in parent):
            raise Conflict("La génération parente est absente du support")
        previous = next(old for old in parent if old.digest == manifest.parent_sha256)
        current = {entry.path: entry for entry in manifest.files}
        if any(current.get(entry.path) != entry for entry in previous.files):
            raise Conflict(
                "Une génération doit conserver toutes les entrées historiques à l'identique"
            )


def plan_copy(
    manifest: Manifest,
    sources: Sources,
    destination: Path | str,
    expected_media_id: str,
    *,
    control: Control | None = None,
) -> dict:
    """Plan en lecture seule. Les existants sont tous relus, jamais approuvés par leur date."""
    manifest.validate()
    control = control or Control()
    control.checkpoint()
    with ExitStack() as stack:
        tree = stack.enter_context(Tree(destination))
        media = read_media(tree, manifest, expected_media_id)
        trees = _source_trees(sources, stack)
        _assert_separate(trees, tree)
        _check_lineage(tree, manifest)
        actions = []
        required = 0
        done = 0
        for entry in manifest.files:
            control.checkpoint()
            result = _inspect(
                tree,
                entry,
                control,
                lambda n, e=entry, d=done: control.emit(
                    "analyse du support", e.path, n, e.size, d + n, manifest.total_bytes
                ),
            )
            action = result.status
            if action == "verified":
                action = "reuse_after_hash"
            elif action == "missing":
                alias, relative = entry.logical_path.split("/", 1)
                source = trees.get(alias)
                if source is None:
                    action = "source_unavailable"
                else:
                    try:
                        digest, size, _ = hash_file(
                            source,
                            relative,
                            control,
                            lambda n, e=entry, d=done: control.emit(
                                "analyse des sources",
                                e.logical_path,
                                n,
                                e.size,
                                d + n,
                                manifest.total_bytes,
                            ),
                        )
                        action = (
                            "copy"
                            if (digest, size) == (entry.sha256, entry.size)
                            else "source_changed"
                        )
                    except Cancelled:
                        raise
                    except (OSError, ColdVaultError):
                        action = "source_unavailable"
                if action == "copy":
                    required += entry.size
            actions.append({"path": entry.path, "action": action, "detail": result.detail})
            done += entry.size
        free = shutil.disk_usage(tree.root).free
        # Marge indicative : ne réserve pas l'espace contre les autres écrivains.
        reserve = max(16 * 1024 * 1024, required // 100)
        expected = {e.path for e in manifest.files}
        extras = [
            path
            for path, info in tree.walk(skip_metadata=True)
            if stat.S_ISREG(info.st_mode) and path not in expected
        ]
        return {
            "archive_id": manifest.archive_id,
            "media_id": media["media_id"],
            "generation": manifest.generation,
            "total_files": manifest.total_files,
            "total_bytes": manifest.total_bytes,
            "copy_bytes": required,
            "free_bytes": free,
            "reserve_bytes": reserve,
            "space_ok": free >= required + reserve,
            "actions": actions,
            "unexpected_files": extras,
            "incomplete_operations": incomplete_operations(tree),
        }


def incomplete_operations(tree: Tree) -> list[dict]:
    jobs = {}
    finished = set()
    for path, info in tree.walk():
        if not path.startswith(".coldvault/operations/") or not stat.S_ISREG(info.st_mode):
            continue
        if path.endswith(".start.json"):
            job = strict_json(tree.read(path))
            if not isinstance(job, dict) or "operation_id" not in job:
                raise ColdVaultError("Journal d'opération invalide")
            jobs[job["operation_id"]] = job
        elif path.endswith(".finish.json"):
            finished.add(Path(path).name.removesuffix(".finish.json"))
    return [job for key, job in jobs.items() if key not in finished]


def _recover(tree: Tree, manifest: Manifest, media_id: str) -> None:
    expected = {entry.path for entry in manifest.files}
    for job in incomplete_operations(tree):
        if (
            job.get("archive_id") != manifest.archive_id
            or job.get("media_id") != media_id
            or job.get("manifest_sha256") != manifest.digest
        ):
            raise Conflict("Journal incomplet associé à une autre identité")
        operation_id = str(uuid.UUID(job["operation_id"]))
        # Un journal n'est pas une permission de déplacer un fichier arbitraire.
        temporaries = job.get("temporaries")
        if not isinstance(temporaries, dict):
            raise ColdVaultError("Liste de temporaires invalide")
        for final, temporary in temporaries.items():
            portable_path(final)
            portable_path(temporary)
            parent, _, name = temporary.rpartition("/")
            if (
                parent != final.rpartition("/")[0]
                or not name.startswith(".cv-")
                or not name.endswith(".part")
                or not final.startswith("data/")
                or final not in expected
                or temporary in expected
            ):
                raise UnsafePath("Chemin temporaire incohérent dans le journal")
            uuid.UUID(name[4:-5])
            if tree.exists(temporary):
                # Conserve chaque résidu ; aucune suppression ni réutilisation de son contenu.
                tree.publish(temporary, f".coldvault/residuals/{uuid.uuid4().hex}.part")
        tree.write_new(
            f".coldvault/operations/{operation_id}.finish.json",
            json_bytes({"status": "recovered", "finished_utc": utc_now()}),
        )


def _transfer(
    source: Tree,
    relative: str,
    destination: Tree,
    temporary: str,
    entry: Entry,
    control: Control,
    done: int,
    total: int,
) -> None:
    with source.open(relative) as incoming, destination.open(temporary, exclusive=True) as outgoing:
        before = os.fstat(incoming.fileno())
        digest = hashlib.sha256()
        count = 0
        while True:
            control.checkpoint()
            chunk = incoming.read(io.CHUNK_SIZE)
            if not chunk:
                break
            count += len(chunk)
            if count > entry.size:
                raise SourceChanged(f"Source agrandie pendant la copie : {entry.logical_path}")
            digest.update(chunk)
            outgoing.write(chunk)
            control.emit("copie", entry.path, count, entry.size, done + count, total)
        outgoing.flush()
        os.fsync(outgoing.fileno())
        after = os.fstat(incoming.fileno())
        if signature(before) != signature(after) or signature(after) != signature(
            source.stat(relative)
        ):
            raise SourceChanged(f"Source modifiée pendant la copie : {entry.logical_path}")
        if count != entry.size or digest.hexdigest() != entry.sha256:
            raise SourceChanged(f"Source différente du manifeste : {entry.logical_path}")
    # Relecture distincte du temporaire après fermeture/synchronisation des écritures.
    digest, size, _ = hash_file(destination, temporary, control)
    if (digest, size) != (entry.sha256, entry.size):
        raise ColdVaultError(f"La copie temporaire est non conforme : {entry.path}")
    control.checkpoint()
    destination.publish(temporary, entry.path)


def copy_archive(
    manifest: Manifest,
    sources: Sources,
    destination: Path | str,
    expected_media_id: str,
    *,
    resume: bool = False,
    control: Control | None = None,
) -> Report:
    manifest.validate()
    control = control or Control()
    control.checkpoint()
    with ExitStack() as stack:
        tree = stack.enter_context(Tree(destination))
        read_media(tree, manifest, expected_media_id)
        stack.enter_context(media_lock(tree))
        read_media(tree, manifest, expected_media_id)
        _check_lineage(tree, manifest)
        if incomplete_operations(tree):
            if not resume:
                raise Conflict("Opération incomplète : examiner le plan puis utiliser --resume")
            _recover(tree, manifest, expected_media_id)
        plan = plan_copy(manifest, sources, destination, expected_media_id, control=control)
        if not plan["space_ok"]:
            raise ColdVaultError("Espace libre insuffisant, marge de sécurité comprise")
        if plan["unexpected_files"]:
            raise Conflict(f"Fichiers supplémentaires à examiner : {plan['unexpected_files']}")
        blocked = [a for a in plan["actions"] if a["action"] not in {"copy", "reuse_after_hash"}]
        if blocked:
            raise Conflict(f"Copie bloquée, aucun existant ne sera écrasé : {blocked}")
        trees = _source_trees(sources, stack)
        _assert_separate(trees, tree)
        operation_id = str(uuid.uuid4())
        to_copy = {a["path"] for a in plan["actions"] if a["action"] == "copy"}
        temporaries = {p: f"{p.rsplit('/', 1)[0]}/.cv-{uuid.uuid4().hex}.part" for p in to_copy}
        job = {
            "operation_id": operation_id,
            "archive_id": manifest.archive_id,
            "media_id": expected_media_id,
            "manifest_sha256": manifest.digest,
            "started_utc": utc_now(),
            "temporaries": temporaries,
        }
        tree.write_new(f".coldvault/operations/{operation_id}.start.json", json_bytes(job))
        done = 0
        try:
            for entry in manifest.files:
                control.checkpoint()
                if entry.path in to_copy:
                    alias, relative = entry.logical_path.split("/", 1)
                    _transfer(
                        trees[alias],
                        relative,
                        tree,
                        temporaries[entry.path],
                        entry,
                        control,
                        done,
                        manifest.total_bytes,
                    )
                done += entry.size
            report = verify(
                manifest,
                tree,
                control=control,
                context="immediate",
                expected_media_id=expected_media_id,
            )
            tree.write_new(f".coldvault/reports/{report.report_id}.json", report.to_bytes())
            if not report.ok:
                raise ColdVaultError(
                    f"Contrôle incomplet ou non conforme ; rapport {report.report_id}"
                )
            manifest_path = f".coldvault/manifests/{manifest.filename}"
            if tree.exists(manifest_path):
                if tree.read(manifest_path) != manifest.to_bytes():
                    raise Conflict("Le manifeste archivé a un contenu différent")
            else:
                tree.write_new(manifest_path, manifest.to_bytes())
            tree.write_new(
                f".coldvault/operations/{operation_id}.finish.json",
                json_bytes(
                    {
                        "status": "verified_immediate",
                        "report_id": report.report_id,
                        "finished_utc": utc_now(),
                    }
                ),
            )
            return report
        except (OSError, ColdVaultError) as exc:
            # Journal de départ conservé : une reprise relira tous les fichiers définitifs.
            try:
                tree.write_new(
                    f".coldvault/reports/{uuid.uuid4()}.json",
                    json_bytes(
                        {
                            "operation_id": operation_id,
                            "ok": False,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "finished_utc": utc_now(),
                            "context": "copy_failed",
                        }
                    ),
                )
            except (OSError, ColdVaultError) as report_error:
                exc.add_note(f"Rapport impossible à enregistrer : {report_error}")
            raise


def record_report(
    manifest: Manifest, destination: Path | str, expected_media_id: str, report: Report
) -> None:
    """Enregistrement explicite d'un contrôle ; ne fait aucune déduction de reconnexion."""
    manifest.validate()
    with Tree(destination) as tree:
        read_media(tree, manifest, expected_media_id)
        with media_lock(tree):
            read_media(tree, manifest, expected_media_id)
            if (
                report.media_id != expected_media_id
                or report.manifest_sha256 != manifest.digest
                or report.archive_id != manifest.archive_id
            ):
                raise Conflict("Rapport associé à un autre manifeste ou support")
            tree.write_new(f".coldvault/reports/{report.report_id}.json", report.to_bytes())
