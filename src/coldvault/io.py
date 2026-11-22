"""E/S prudentes : descripteurs relatifs POSIX et publication sans remplacement."""

import contextlib
import ctypes
import errno
import hashlib
import os
import stat
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from .control import Control
from .errors import ColdVaultError, SourceChanged, UnsafePath
from .manifest import portable_path

CHUNK_SIZE = 1024 * 1024
POSIX = os.name == "posix"


def is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def signature(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _sync_directory(descriptor: int) -> bool:
    try:
        os.fsync(descriptor)
        return True
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.ENOTSUP, errno.EBADF):
            return False
        raise


class Tree:
    """Racine existante ; refuse liens, jonctions et fichiers spéciaux.

    POSIX : parcours relatif à des descripteurs O_NOFOLLOW, y compris pendant les écritures.
    Windows : contrôle des reparse points ; exige l'absence d'un autre acteur modifiant l'arbre.
    """

    def __init__(self, root: Path | str):
        selected = Path(root).absolute()
        if is_link(selected.lstat()):
            raise UnsafePath("Une racine ne peut pas être un lien ou une jonction")
        self.root = selected.resolve(strict=True)
        self.fd = (
            os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW) if POSIX else None
        )
        if not self.root.is_dir():
            raise UnsafePath("Un dossier existant est requis")
        info = os.fstat(self.fd) if POSIX else self.root.stat()
        self.root_id = (info.st_dev, info.st_ino)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)

    def check_root(self) -> None:
        info = self.root.lstat()
        if is_link(info) or (info.st_dev, info.st_ino) != self.root_id:
            raise UnsafePath("Le dossier racine a été remplacé pendant l'opération")

    @contextlib.contextmanager
    def parent(self, relative: str, create: bool = False):
        parts = portable_path(relative)
        if POSIX:
            descriptor = os.dup(self.fd)
            try:
                for part in parts[:-1]:
                    if create:
                        try:
                            os.mkdir(part, dir_fd=descriptor)
                            _sync_directory(descriptor)
                        except FileExistsError:
                            pass
                    following = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                    )
                    os.close(descriptor)
                    descriptor = following
                yield descriptor, parts[-1]
            finally:
                os.close(descriptor)
        else:
            current = self.root
            for part in parts[:-1]:
                current /= part
                if create:
                    current.mkdir(exist_ok=True)
                info = current.lstat()
                if is_link(info) or not stat.S_ISDIR(info.st_mode):
                    raise UnsafePath(f"Dossier non sûr : {relative}")
            target = current / parts[-1]
            if len(str(target)) >= 260:
                raise UnsafePath("Chemin absolu Windows trop long pour cette version")
            yield None, target

    def open(self, relative: str, *, exclusive: bool = False, writable: bool = False) -> BinaryIO:
        with self.parent(relative, create=exclusive) as (descriptor, name):
            flags = os.O_RDWR if writable else os.O_RDONLY
            if exclusive:
                flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_NONBLOCK", 0)
            if not POSIX and not exclusive:
                if is_link(Path(name).lstat()):
                    raise UnsafePath(f"Lien ou jonction interdit : {relative}")
            fd = os.open(name, flags, 0o600, dir_fd=descriptor)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise UnsafePath(f"Fichier non régulier : {relative}")
                return os.fdopen(fd, "r+b" if exclusive or writable else "rb")
            except BaseException:
                os.close(fd)
                raise

    def stat(self, relative: str) -> os.stat_result:
        with self.parent(relative) as (descriptor, name):
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if is_link(info):
                raise UnsafePath(f"Lien interdit : {relative}")
            return info

    def exists(self, relative: str) -> bool:
        try:
            self.stat(relative)
            return True
        except FileNotFoundError:
            return False

    def read(self, relative: str) -> bytes:
        with self.open(relative) as stream:
            return stream.read()

    def walk(self, *, skip_metadata: bool = False) -> Iterator[tuple[str, os.stat_result]]:
        def visit(base, prefix):
            with os.scandir(base) as listing:
                names = sorted(entry.name for entry in listing)
            for name in names:
                if not prefix and skip_metadata and name == ".coldvault":
                    continue
                relative = f"{prefix}/{name}" if prefix else name
                # Ne pas normaliser les noms : le manifeste conserve les octets Unicode décodés.
                with self.parent(relative) as (parent_fd, target):
                    info = os.stat(target, dir_fd=parent_fd, follow_symlinks=False)
                if is_link(info):
                    raise UnsafePath(f"Lien ou jonction interdit : {relative}")
                if stat.S_ISDIR(info.st_mode):
                    yield relative, info
                    if POSIX:
                        with self.parent(relative + "/placeholder") as (child, _):
                            yield from visit(child, relative)
                    else:
                        yield from visit(self.root / relative, relative)
                elif stat.S_ISREG(info.st_mode):
                    yield relative, info
                else:
                    raise UnsafePath(f"Fichier spécial interdit : {relative}")

        yield from visit(self.fd if POSIX else self.root, "")

    def sync_parent(self, relative: str) -> bool:
        if not POSIX:
            return False
        with self.parent(relative) as (descriptor, _):
            return _sync_directory(descriptor)

    def publish(self, temporary: str, final: str) -> None:
        """Renommage natif sans remplacement. Aucun fallback qui écrase."""
        self.check_root()
        with self.parent(temporary) as (source_fd, source):
            with self.parent(final, create=True) as (target_fd, target):
                if os.name == "nt":
                    os.rename(source, target)  # Windows refuse une destination existante.
                else:
                    libc = ctypes.CDLL(None, use_errno=True)
                    if sys.platform == "darwin":
                        rename = getattr(libc, "renameatx_np", None)
                        flag = 0x00000004  # RENAME_EXCL, <stdio.h> Darwin.
                    elif sys.platform.startswith("linux"):
                        rename = getattr(libc, "renameat2", None)
                        flag = 1  # RENAME_NOREPLACE.
                    else:
                        rename = None
                    if rename is None:
                        raise ColdVaultError(
                            "Publication sans remplacement indisponible sur cet OS"
                        )
                    rename.argtypes = [
                        ctypes.c_int,
                        ctypes.c_char_p,
                        ctypes.c_int,
                        ctypes.c_char_p,
                        ctypes.c_uint,
                    ]
                    rename.restype = ctypes.c_int
                    if rename(source_fd, os.fsencode(source), target_fd, os.fsencode(target), flag):
                        number = ctypes.get_errno()
                        raise OSError(number, os.strerror(number), final)
        self.sync_parent(final)
        if temporary.rsplit("/", 1)[0] != final.rsplit("/", 1)[0]:
            self.sync_parent(temporary)

    def write_new(self, relative: str, data: bytes) -> None:
        self.check_root()
        if self.exists(relative):
            raise FileExistsError(errno.EEXIST, "Le fichier existe déjà", relative)
        parent = relative.rsplit("/", 1)[0] if "/" in relative else ""
        temporary = f"{parent + '/' if parent else ''}.cv-{uuid.uuid4().hex}.part"
        with self.open(temporary, exclusive=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if self.read(temporary) != data:
            raise ColdVaultError(f"Métadonnées temporaires non conformes : {relative}")
        # Le temporaire est conservé en cas d'échec pour diagnostic/reprise.
        self.publish(temporary, relative)
        if self.read(relative) != data:
            raise ColdVaultError(f"Métadonnées publiées non conformes : {relative}")


def hash_file(
    tree: Tree, path: str, control: Control | None = None, on_chunk=None
) -> tuple[str, int, int]:
    control = control or Control()
    with tree.open(path) as stream:
        before = os.fstat(stream.fileno())
        digest = hashlib.sha256()
        size = 0
        while True:
            control.checkpoint()
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            if size > before.st_size:
                raise SourceChanged(f"Fichier agrandi pendant la lecture : {path}")
            if on_chunk:
                on_chunk(size)
        after = os.fstat(stream.fileno())
        assert_file_unchanged(tree, path, before, after)
        if size != before.st_size:
            raise SourceChanged(f"Longueur modifiée pendant la lecture : {path}")
        return digest.hexdigest(), size, before.st_mtime_ns


def assert_file_unchanged(
    tree: Tree, path: str, before: os.stat_result, after: os.stat_result
) -> None:
    """Compare des résultats fstat uniquement, y compris pour le nom courant.

    Sous Windows, stat(path).st_ctime_ns peut être la date de création alors que
    fstat(fd).st_ctime_ns est la date de changement (CPython #157671).
    La réouverture sûre contrôle aussi l'identité du fichier toujours désigné par
    le chemin, sans abandonner les contrôles de ctime, mtime, taille ou inode.
    """
    if signature(before) != signature(after):
        raise SourceChanged(f"Fichier modifié pendant la lecture : {path}")
    with tree.open(path) as current:
        if signature(after) != signature(os.fstat(current.fileno())):
            raise SourceChanged(f"Fichier modifié ou remplacé pendant la lecture : {path}")


@contextlib.contextmanager
def media_lock(tree: Tree):
    """Verrou consultatif libéré par l'OS après crash ; le fichier reste sur le support."""
    path = ".coldvault/operation.lock"
    try:
        stream = tree.open(path, exclusive=True)
        stream.write(b"0")
        stream.flush()
    except FileExistsError:
        stream = tree.open(path, writable=True)
    with stream:
        try:
            if POSIX:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ColdVaultError("Support utilisé par une autre opération") from exc
        yield
