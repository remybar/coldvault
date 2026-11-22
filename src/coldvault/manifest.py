"""Format public v1. Aucun chemin absolu ni donnée spécifique à un OS."""

import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from .errors import ColdVaultError, UnsafePath

FORMAT = 1
RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
RESERVED |= {f"{kind}{i}" for kind in ("COM", "LPT") for i in "123456789¹²³"}
HEX = re.compile(r"[0-9a-f]{64}\Z")


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def portable_path(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        raise UnsafePath("Chemin relatif vide ou invalide")
    parts = value.split("/")
    for part in parts:
        if not part or part in (".", "..") or part.endswith((" ", ".")):
            raise UnsafePath(f"Composant de chemin interdit : {value!r}")
        if any(c in '<>:"\\|?*' or ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in part):
            raise UnsafePath(f"Nom incompatible : {value!r}")
        if part.split(".")[0].rstrip(" ").upper() in RESERVED:
            raise UnsafePath(f"Nom réservé Windows : {value!r}")
        if len(part.encode("utf-16-le")) // 2 > 255 or len(part.encode("utf-8")) > 255:
            raise UnsafePath(f"Nom trop long : {value!r}")
    # Marge conservatrice pour les chemins Windows traditionnels ; préfixe absolu testé à la copie.
    if len(value.encode("utf-16-le")) // 2 > 220:
        raise UnsafePath(f"Chemin relatif trop long (limite conservatrice 220) : {value!r}")
    return tuple(parts)


def path_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def check_namespace(paths: list[str]) -> None:
    """Inclut les préfixes : A/x et a/y sont aussi un conflit de dossiers."""
    seen: dict[str, str] = {}
    files = set(paths)
    for path in paths:
        parts = portable_path(path)
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            key = path_key(prefix)
            previous = seen.setdefault(key, prefix)
            if previous != prefix:
                raise UnsafePath(f"Conflit de casse/Unicode : {previous!r}, {prefix!r}")
            if i < len(parts) and prefix in files:
                raise UnsafePath(f"Un fichier est aussi utilisé comme dossier : {prefix!r}")
    if len(files) != len(paths):
        raise UnsafePath("Chemins dupliqués")


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def strict_json(data: bytes) -> object:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ColdVaultError(f"Clé JSON dupliquée : {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)),
        )
    except (ValueError, UnicodeError) as exc:
        raise ColdVaultError(f"JSON invalide : {exc}") from exc


@dataclass(frozen=True)
class Entry:
    path: str
    logical_path: str
    size: int
    sha256: str
    revision: int
    mtime_ns: int | None = None


@dataclass(frozen=True)
class Manifest:
    format_version: int
    archive_id: str
    name: str
    generation: int
    created_utc: str
    roots: tuple[str, ...]
    files: tuple[Entry, ...]
    total_files: int
    total_bytes: int
    parent_sha256: str | None = None

    def validate(self) -> None:
        try:
            if type(self.format_version) is not int or self.format_version != FORMAT:
                raise ValueError("Version de format non prise en charge")
            if str(uuid.UUID(self.archive_id)) != self.archive_id:
                raise ValueError("Identifiant d'archive non canonique")
            if not isinstance(self.name, str) or not self.name.strip():
                raise ValueError("Nom d'archive requis")
            if type(self.generation) is not int or not 1 <= self.generation <= 999999:
                raise ValueError("Génération invalide")
            if not self.created_utc.endswith("Z"):
                raise ValueError("Date UTC requise")
            datetime.fromisoformat(self.created_utc.replace("Z", "+00:00"))
            if not self.roots or any(len(portable_path(r)) != 1 for r in self.roots):
                raise ValueError("Racines logiques invalides")
            check_namespace(list(self.roots))
            if self.generation == 1 and self.parent_sha256 is not None:
                raise ValueError("La première génération ne possède pas de parent")
            if self.generation > 1 and not HEX.fullmatch(self.parent_sha256 or ""):
                raise ValueError("Empreinte du parent requise")
            for entry in self.files:
                logical = portable_path(entry.logical_path)
                portable_path(entry.path)
                if len(logical) < 2 or logical[0] not in self.roots:
                    raise ValueError("Racine inconnue")
                if type(entry.revision) is not int or not 1 <= entry.revision <= self.generation:
                    raise ValueError("Révision invalide")
                if entry.path != f"data/{entry.revision:06d}/{entry.logical_path}":
                    raise ValueError("Chemin physique incompatible avec la révision")
                if type(entry.size) is not int or entry.size < 0:
                    raise ValueError("Taille invalide")
                if not isinstance(entry.sha256, str) or not HEX.fullmatch(entry.sha256):
                    raise ValueError("Empreinte SHA-256 invalide")
                if entry.mtime_ns is not None and type(entry.mtime_ns) is not int:
                    raise ValueError("Horodatage invalide")
            check_namespace([e.path for e in self.files])
            # Les noms logiques historiques peuvent devenir des dossiers dans une autre génération.
            names = list(dict.fromkeys(e.logical_path for e in self.files))
            spellings: dict[str, str] = {}
            for name in names:
                for i in range(1, len(name.split("/")) + 1):
                    prefix = "/".join(name.split("/")[:i])
                    if spellings.setdefault(path_key(prefix), prefix) != prefix:
                        raise ValueError("Conflit de casse entre générations")
            if type(self.total_files) is not int or self.total_files != len(self.files):
                raise ValueError("Nombre total incohérent")
            if type(self.total_bytes) is not int or self.total_bytes != sum(
                e.size for e in self.files
            ):
                raise ValueError("Taille totale incohérente")
        except (ValueError, TypeError, AttributeError) as exc:
            raise ColdVaultError(f"Manifeste invalide : {exc}") from exc

    def to_bytes(self) -> bytes:
        self.validate()
        return json_bytes(asdict(self))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    @property
    def filename(self) -> str:
        return f"g{self.generation:06d}-{self.digest}.json"

    @classmethod
    def from_bytes(cls, data: bytes) -> "Manifest":
        value = strict_json(data)
        try:
            if not isinstance(value, dict):
                raise TypeError("Objet JSON attendu")
            if not isinstance(value["files"], list) or not isinstance(value["roots"], list):
                raise TypeError("files et roots doivent être des tableaux JSON")
            value["files"] = tuple(Entry(**entry) for entry in value["files"])
            value["roots"] = tuple(value["roots"])
            result = cls(**value)
            result.validate()
            return result
        except (TypeError, KeyError) as exc:
            raise ColdVaultError(f"Structure de manifeste invalide : {exc}") from exc
