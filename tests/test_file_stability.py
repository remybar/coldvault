"""Régressions des contrôles de stabilité, notamment stat/fstat sous Windows."""

import hashlib
from types import SimpleNamespace

import pytest

from coldvault.engine import copy_archive
from coldvault.errors import SourceChanged
from coldvault.io import Tree, hash_file


def different_path_ctime(monkeypatch):
    original = Tree.stat

    def stat_with_birthtime(tree, relative):
        info = original(tree, relative)
        # CPython Windows peut exposer la date de création via stat et la date
        # de changement via fstat : mêmes octets/identité, ctime différents.
        return SimpleNamespace(
            st_dev=info.st_dev,
            st_ino=info.st_ino,
            st_mode=info.st_mode,
            st_size=info.st_size,
            st_mtime_ns=info.st_mtime_ns,
            st_ctime_ns=info.st_ctime_ns - 1_000_000,
        )

    monkeypatch.setattr(Tree, "stat", stat_with_birthtime)


def test_hash_accepts_stable_file_with_different_stat_and_fstat_ctime(tmp_path, monkeypatch):
    payload = b"stable file"
    (tmp_path / "file").write_bytes(payload)
    different_path_ctime(monkeypatch)
    with Tree(tmp_path) as tree:
        digest, size, _ = hash_file(tree, "file")
    assert (digest, size) == (hashlib.sha256(payload).hexdigest(), len(payload))


def test_copy_accepts_stable_files_with_different_stat_and_fstat_ctime(archive, monkeypatch):
    sources, manifest, destination, media_id = archive
    different_path_ctime(monkeypatch)
    assert copy_archive(manifest, sources, destination, media_id).ok


def test_hash_rejects_path_replaced_with_identical_bytes(tmp_path):
    path = tmp_path / "file"
    replacement = tmp_path / "replacement"
    path.write_bytes(b"same bytes")
    replacement.write_bytes(b"same bytes")

    def replace_after_read(_):
        try:
            replacement.replace(path)
        except PermissionError:
            pytest.skip("Windows refuse le remplacement d'un fichier ouvert")

    with Tree(tmp_path) as tree, pytest.raises(SourceChanged):
        hash_file(tree, "file", on_chunk=replace_after_read)
