import errno
import hashlib
import json
import os
import threading
from dataclasses import replace

import pytest

from coldvault import io
from coldvault.control import Control
from coldvault.engine import (
    copy_archive,
    create_manifest,
    incomplete_operations,
    initialize_media,
    plan_copy,
    record_report,
    source_comparison,
    update_summary,
    verify,
)
from coldvault.errors import Cancelled, ColdVaultError, Conflict, SourceChanged
from coldvault.io import Tree, hash_file, media_lock


def test_copy_then_independent_verification(archive):
    sources, manifest, destination, media_id = archive
    before = sorted(str(p.relative_to(destination)) for p in destination.rglob("*"))
    plan = plan_copy(manifest, sources, destination, media_id)
    assert {a["action"] for a in plan["actions"]} == {"copy"}
    assert plan["copy_bytes"] == manifest.total_bytes
    assert before == sorted(str(p.relative_to(destination)) for p in destination.rglob("*"))
    report = copy_archive(manifest, sources, destination, media_id)
    assert report.ok and report.context == "immediate"
    assert report.reconnection_evidence == "none"
    assert (
        destination / ".coldvault/manifests" / manifest.filename
    ).read_bytes() == manifest.to_bytes()
    for source in sources.values():
        source.rename(source.with_name(source.name + "-disconnected"))
    checked = verify(manifest, destination)
    assert checked.ok and len(checked.results) == 3


def test_second_copy_is_independent(archive, tmp_path):
    sources, manifest, a, a_id = archive
    b = tmp_path / "HDD-B"
    b.mkdir()
    b_id = initialize_media(b, manifest, "Grand-mère")["media_id"]
    assert a_id != b_id
    copy_archive(manifest, sources, a, a_id)
    copy_archive(manifest, sources, b, b_id)
    for entry in manifest.files:
        assert (a / entry.path).read_bytes() == (b / entry.path).read_bytes()
    (a / manifest.files[0].path).write_bytes(b"bad")
    assert not verify(manifest, a).ok
    assert verify(manifest, b).ok


def test_corruption_missing_size_extra(archive):
    sources, manifest, destination, media_id = archive
    copy_archive(manifest, sources, destination, media_id)
    by_name = {e.logical_path: e for e in manifest.files}
    photo = destination / by_name["Photos/été 東京.jpg"].path
    data = bytearray(photo.read_bytes())
    data[15] ^= 1
    photo.write_bytes(data)
    (destination / by_name["Photos/vide"].path).unlink()  # Simulation utilisateur, pas moteur.
    (destination / by_name["Documents/papier.txt"].path).write_bytes(b"different length")
    (destination / "inattendu.txt").write_bytes(b"extra")
    report = verify(manifest, destination)
    assert not report.ok
    assert {r.status for r in report.results} == {
        "missing",
        "hash_mismatch",
        "size_mismatch",
        "extra",
    }
    assert "corrupt" not in report.to_bytes().decode()
    with pytest.raises(Conflict):
        copy_archive(manifest, sources, destination, media_id)
    assert photo.read_bytes() == data


def test_existing_valid_file_is_rehashed_not_recopied(archive, monkeypatch):
    sources, manifest, destination, media_id = archive
    copy_archive(manifest, sources, destination, media_id)
    import coldvault.engine as engine

    def no_transfer(*args):
        pytest.fail("Un fichier intègre ne doit pas être recopié")

    monkeypatch.setattr(engine, "_transfer", no_transfer)
    assert copy_archive(manifest, {}, destination, media_id).ok


def test_cancel_and_resume_preserves_residuals(archive, monkeypatch):
    sources, manifest, destination, media_id = archive
    monkeypatch.setattr(io, "CHUNK_SIZE", 10)
    control = Control()
    control.callback = lambda event: control.cancel() if event.phase == "copie" else None
    with pytest.raises(Cancelled):
        copy_archive(manifest, sources, destination, media_id, control=control)
    with Tree(destination) as tree:
        assert len(incomplete_operations(tree)) == 1
    assert list(destination.glob("data/**/.cv-*.part"))
    assert not list(destination.glob(".coldvault/manifests/*.json"))
    with pytest.raises(Conflict):
        copy_archive(manifest, sources, destination, media_id)
    report = copy_archive(manifest, sources, destination, media_id, resume=True)
    assert report.ok
    assert list(destination.glob(".coldvault/residuals/*.part"))
    assert not list(destination.glob("data/**/.cv-*.part"))
    with Tree(destination) as tree:
        assert not incomplete_operations(tree)


def test_cancel_verification_marks_every_file(archive):
    sources, manifest, destination, media_id = archive
    copy_archive(manifest, sources, destination, media_id)
    control = Control()
    control.cancel()
    report = verify(manifest, destination, control=control)
    assert not report.ok
    assert len(report.results) == manifest.total_files
    assert all(r.status == "not_verified" for r in report.results)


def test_cancel_empty_verification_does_not_pass(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    manifest = create_manifest({"Empty": source}, "Empty")
    control = Control()
    control.cancel()
    assert not verify(manifest, target, control=control).ok


def test_source_changed_during_hash(archive, monkeypatch):
    sources, _, _, _ = archive
    monkeypatch.setattr(io, "CHUNK_SIZE", 10)
    path = sources["Photos"] / "été 東京.jpg"
    touched = False

    def mutate(_):
        nonlocal touched
        if not touched:
            with path.open("r+b") as stream:
                stream.write(b"X")
                stream.flush()
                os.fsync(stream.fileno())
            touched = True

    with Tree(sources["Photos"]) as tree, pytest.raises(SourceChanged):
        hash_file(tree, path.name, on_chunk=mutate)


def test_source_changed_during_copy(archive, monkeypatch):
    sources, manifest, destination, media_id = archive
    monkeypatch.setattr(io, "CHUNK_SIZE", 10)
    changed = False

    def mutate(event):
        nonlocal changed
        if event.phase == "copie" and not changed:
            alias, relative = next(
                e.logical_path for e in manifest.files if e.path == event.path
            ).split("/", 1)
            with (sources[alias] / relative).open("ab") as stream:
                stream.write(b"changed")
            changed = True

    with pytest.raises(SourceChanged):
        copy_archive(manifest, sources, destination, media_id, control=Control(mutate))
    assert not list(destination.glob(".coldvault/manifests/*.json"))


def test_streaming_large_file(tmp_path, monkeypatch):
    monkeypatch.setattr(io, "CHUNK_SIZE", 4096)
    path = tmp_path / "big.bin"
    block = b"12345678" * 1024
    digest = hashlib.sha256()
    with path.open("wb") as stream:
        for _ in range(2048):
            stream.write(block)
            digest.update(block)
    chunks = []
    with Tree(tmp_path) as tree:
        result = hash_file(tree, path.name, on_chunk=chunks.append)
    assert result[:2] == (digest.hexdigest(), 16 * 1024 * 1024)
    assert len(chunks) == 4096
    assert max(b - a for a, b in zip([0, *chunks], chunks, strict=False)) <= 4096


def test_space_preflight(archive, monkeypatch):
    sources, manifest, destination, media_id = archive
    import shutil

    usage = shutil.disk_usage(destination)
    monkeypatch.setattr(shutil, "disk_usage", lambda _: usage._replace(free=0))
    with pytest.raises(ColdVaultError, match="Espace libre"):
        copy_archive(manifest, sources, destination, media_id)
    assert not (destination / "data").exists()


def test_enospc_during_copy(archive, monkeypatch):
    sources, manifest, destination, media_id = archive
    import coldvault.engine as engine

    def fail_transfer(source, relative, tree, temporary, *args):
        with tree.open(temporary, exclusive=True) as stream:
            stream.write(b"partial")
        raise OSError(errno.ENOSPC, "Disk full")

    monkeypatch.setattr(engine, "_transfer", fail_transfer)
    with pytest.raises(OSError) as exc:
        copy_archive(manifest, sources, destination, media_id)
    assert exc.value.errno == errno.ENOSPC
    assert not list(destination.glob(".coldvault/manifests/*.json"))
    assert list(destination.glob("data/**/.cv-*.part"))


def test_unreadable_source(archive, monkeypatch):
    sources, _, _, _ = archive
    real_open = Tree.open

    def denied(tree, path, **kwargs):
        if tree.root == sources["Photos"]:
            raise PermissionError("Accès refusé simulé")
        return real_open(tree, path, **kwargs)

    monkeypatch.setattr(Tree, "open", denied)
    with pytest.raises(PermissionError):
        create_manifest(sources, "Inaccessible")


def test_unreadable_destination_report(archive, monkeypatch):
    sources, manifest, destination, media_id = archive
    copy_archive(manifest, sources, destination, media_id)
    real_open = Tree.open

    def denied(tree, path, **kwargs):
        if path == manifest.files[0].path:
            raise PermissionError("Erreur E/S simulée")
        return real_open(tree, path, **kwargs)

    monkeypatch.setattr(Tree, "open", denied)
    assert any(r.status == "io_error" for r in verify(manifest, destination).results)


def test_wrong_media_id_causes_no_write(archive):
    sources, manifest, destination, _ = archive
    (destination / ".coldvault/operation.lock").unlink()
    before = {p: p.read_bytes() for p in destination.rglob("*") if p.is_file()}
    with pytest.raises(ColdVaultError):
        copy_archive(manifest, sources, destination, "wrong-id")
    assert before == {p: p.read_bytes() for p in destination.rglob("*") if p.is_file()}


def test_initialization_refuses_existing_data(archive):
    _, manifest, destination, _ = archive
    with pytest.raises(Conflict):
        initialize_media(destination, manifest, "Another")


def test_update_retains_deleted_and_modified_versions(archive):
    sources, previous, destination, media_id = archive
    copy_archive(previous, sources, destination, media_id)
    old_files = {e.path: (destination / e.path).read_bytes() for e in previous.files}
    (sources["Photos"] / "été 東京.jpg").write_bytes(b"nouvelle version")
    (sources["Photos"] / "nouveau.jpg").write_bytes(b"nouveau")
    (sources["Documents"] / "papier.txt").unlink()
    candidate = create_manifest(sources, previous.name, previous=previous)
    assert candidate.generation == 2
    assert candidate.parent_sha256 == previous.digest
    assert candidate.total_files == 5
    summary = update_summary(previous, candidate)
    assert summary["new"] == ["Photos/nouveau.jpg"]
    assert summary["modified"] == ["Photos/été 東京.jpg"]
    comparison = source_comparison(candidate, sources)
    assert comparison["deleted_from_sources"] == ["Documents/papier.txt"]
    assert comparison["unchanged"] == ["Photos/vide"]
    assert copy_archive(candidate, sources, destination, media_id).ok
    for path, content in old_files.items():
        assert (destination / path).read_bytes() == content
    assert len(list(destination.glob(".coldvault/manifests/*.json"))) == 2
    assert verify(candidate, destination).ok


def test_corrupt_historical_file_blocks_update(archive):
    sources, previous, destination, media_id = archive
    copy_archive(previous, sources, destination, media_id)
    (sources["Photos"] / "new").write_bytes(b"new")
    candidate = create_manifest(sources, previous.name, previous=previous)
    (destination / previous.files[0].path).write_bytes(b"wrong")
    with pytest.raises(Conflict):
        copy_archive(candidate, sources, destination, media_id)
    assert not (destination / "data/000002").exists()


def test_divergent_generation_is_blocked(archive):
    sources, previous, destination, media_id = archive
    copy_archive(previous, sources, destination, media_id)
    with pytest.raises(Conflict):
        copy_archive(replace(previous, name="different"), sources, destination, media_id)


def test_reconnection_is_explicit_user_declaration(archive):
    sources, manifest, destination, media_id = archive
    copy_archive(manifest, sources, destination, media_id)
    with pytest.raises(ColdVaultError):
        verify(manifest, destination, context="after_reconnection")
    report = verify(
        manifest,
        destination,
        expected_media_id=media_id,
        context="after_reconnection",
        user_declared_reconnected=True,
    )
    assert report.ok and report.reconnection_evidence == "user_declaration"
    record_report(manifest, destination, media_id, report)
    stored = json.loads((destination / f".coldvault/reports/{report.report_id}.json").read_bytes())
    assert stored["context"] == "after_reconnection"


def test_pause_and_cancel_are_cooperative():
    control = Control()
    control.pause()
    completed = threading.Event()

    def worker():
        control.checkpoint()
        completed.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert not completed.wait(0.05)
    control.resume()
    assert completed.wait(2)
    thread.join(timeout=2)


def test_media_lock_is_exclusive(archive):
    _, _, destination, _ = archive
    with Tree(destination) as a, Tree(destination) as b, media_lock(a):
        with pytest.raises(ColdVaultError, match="autre opération"), media_lock(b):
            pytest.fail("Verrou obtenu deux fois")
