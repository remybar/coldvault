import errno
import os
import stat
from dataclasses import replace

import pytest

from coldvault.control import Control
from coldvault.engine import copy_archive, create_manifest, plan_copy, verify
from coldvault.errors import ColdVaultError, Conflict, UnsafePath
from coldvault.io import POSIX, Tree, hash_file


def symlink_or_skip(link, target, is_dir=False):
    try:
        link.symlink_to(target, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("Privilège de création de lien symbolique indisponible")


def test_source_symlink_is_never_followed(archive, tmp_path):
    sources, _, _, _ = archive
    secret = tmp_path / "outside"
    secret.write_bytes(b"outside")
    symlink_or_skip(sources["Photos"] / "link", secret)
    with pytest.raises(UnsafePath):
        create_manifest(sources, "Links")
    assert secret.read_bytes() == b"outside"


def test_root_symlink_is_rejected(archive, tmp_path):
    sources, _, _, _ = archive
    link = tmp_path / "linked-root"
    symlink_or_skip(link, sources["Photos"], is_dir=True)
    with pytest.raises(UnsafePath):
        create_manifest({"Photos": link}, "Links")


def test_target_symlink_cannot_escape(archive, tmp_path):
    sources, manifest, destination, media_id = archive
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_or_skip(destination / "data", outside, is_dir=True)
    with pytest.raises((OSError, ColdVaultError)):
        copy_archive(manifest, sources, destination, media_id)
    assert not list(outside.iterdir())


def test_manifest_symlink_file_fails_verification(archive, tmp_path):
    sources, manifest, destination, media_id = archive
    copy_archive(manifest, sources, destination, media_id)
    entry = manifest.files[0]
    outside = tmp_path / "replacement"
    outside.write_bytes((destination / entry.path).read_bytes())
    (destination / entry.path).unlink()
    symlink_or_skip(destination / entry.path, outside)
    report = verify(manifest, destination)
    assert not report.ok
    assert any(r.path == entry.path and r.status == "io_error" for r in report.results)


def test_publish_race_never_overwrites(archive, monkeypatch):
    sources, manifest, destination, media_id = archive
    original = Tree.publish
    target = manifest.files[0].path

    def competitor(tree, temporary, final):
        if final == target:
            with tree.open(final, exclusive=True) as stream:
                stream.write(b"created by someone else")
        return original(tree, temporary, final)

    monkeypatch.setattr(Tree, "publish", competitor)
    with pytest.raises(FileExistsError):
        copy_archive(manifest, sources, destination, media_id)
    assert (destination / target).read_bytes() == b"created by someone else"
    assert not list(destination.glob(".coldvault/manifests/*.json"))


def test_failure_after_publication_resumes_by_rehashing(archive, monkeypatch):
    sources, manifest, destination, media_id = archive
    original = Tree.publish
    stopped = False

    def crash(tree, temporary, final):
        nonlocal stopped
        original(tree, temporary, final)
        if final.startswith("data/") and not stopped:
            stopped = True
            raise OSError(errno.EIO, "Crash simulé après publication")

    monkeypatch.setattr(Tree, "publish", crash)
    with pytest.raises(OSError):
        copy_archive(manifest, sources, destination, media_id)
    assert (destination / manifest.files[0].path).exists()
    monkeypatch.setattr(Tree, "publish", original)
    plan = plan_copy(manifest, sources, destination, media_id)
    assert plan["actions"][0]["action"] == "reuse_after_hash"
    assert copy_archive(manifest, sources, destination, media_id, resume=True).ok


def test_unknown_residual_is_reported_not_deleted(archive):
    sources, manifest, destination, media_id = archive
    copy_archive(manifest, sources, destination, media_id)
    residual = destination / "data/000001/Photos/.cv-unknown.part"
    residual.write_bytes(b"unknown")
    assert any(r.status == "temporary" for r in verify(manifest, destination).results)
    with pytest.raises(Conflict, match="supplémentaires"):
        copy_archive(manifest, sources, destination, media_id, resume=True)
    assert residual.read_bytes() == b"unknown"


def test_source_and_target_must_not_overlap(archive):
    _, manifest, destination, media_id = archive
    with pytest.raises(UnsafePath, match="distincts"):
        copy_archive(manifest, {"Photos": destination}, destination, media_id)


def test_update_requires_all_previous_roots(archive):
    sources, previous, _, _ = archive
    with pytest.raises(ColdVaultError, match="racines antérieures"):
        create_manifest({"Photos": sources["Photos"]}, "Famille", previous=previous)


def test_new_file_can_replace_a_source_directory_without_losing_history(archive):
    sources, previous, destination, media_id = archive
    copy_archive(previous, sources, destination, media_id)
    old_file = sources["Photos"] / "vide"
    old_file.unlink()
    old_file.mkdir()
    (old_file / "nested.txt").write_bytes(b"nested")
    candidate = create_manifest(sources, previous.name, previous=previous)
    assert candidate.total_files == previous.total_files + 1
    assert copy_archive(candidate, sources, destination, media_id).ok


def test_traversal_fails_before_any_write(archive, tmp_path):
    sources, manifest, destination, media_id = archive
    entry = replace(manifest.files[0], path="../outside")
    bad = replace(manifest, files=(entry, *manifest.files[1:]))
    with pytest.raises(ColdVaultError):
        copy_archive(bad, sources, destination, media_id)
    assert not (tmp_path / "outside").exists()


def test_fsync_failure_does_not_validate_payload(archive, monkeypatch):
    sources, manifest, destination, media_id = archive
    real_sync = os.fsync

    def fail(fd):
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode) and info.st_size == manifest.files[0].size:
            raise OSError(errno.EIO, "Écriture non synchronisée")
        return real_sync(fd)

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError):
        copy_archive(manifest, sources, destination, media_id)
    assert not (destination / manifest.files[0].path).exists()
    assert not list(destination.glob(".coldvault/manifests/*.json"))


def test_primary_failure_survives_failed_error_report(archive, monkeypatch):
    sources, manifest, destination, media_id = archive
    real_open = Tree.open

    def unplugged(tree, relative, **kwargs):
        if relative.startswith("data/"):
            raise OSError(errno.ENODEV, "Disque débranché")
        if relative.startswith(".coldvault/reports/"):
            raise OSError(errno.EIO, "Rapport impossible")
        return real_open(tree, relative, **kwargs)

    monkeypatch.setattr(Tree, "open", unplugged)
    with pytest.raises(OSError) as exc:
        copy_archive(manifest, sources, destination, media_id)
    assert exc.value.errno == errno.ENODEV
    assert "Rapport impossible" in exc.value.__notes__[0]


@pytest.mark.skipif(not POSIX, reason="Parcours par descripteurs POSIX")
def test_parent_replaced_by_symlink_cannot_escape(archive, tmp_path):
    sources, _, _, _ = archive
    root = sources["Photos"]
    parent = root / "folder"
    parent.mkdir()
    (parent / "file").write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_bytes(b"secret")
    with Tree(root) as tree:
        parent.rename(root / "old-folder")
        parent.symlink_to(outside, target_is_directory=True)
        with pytest.raises(OSError):
            hash_file(tree, "folder/file")


@pytest.mark.skipif(not POSIX, reason="FIFO POSIX")
def test_special_file_never_blocks_scanning(archive):
    sources, _, _, _ = archive
    os.mkfifo(sources["Photos"] / "pipe")
    with pytest.raises(UnsafePath, match="spécial"):
        create_manifest(sources, "FIFO", control=Control())
