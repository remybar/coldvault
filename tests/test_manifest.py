import hashlib
import json
from dataclasses import replace

import pytest

from coldvault.engine import create_manifest, export_new, load_manifest
from coldvault.errors import ColdVaultError, UnsafePath
from coldvault.io import Tree, hash_file
from coldvault.manifest import Manifest, check_namespace, portable_path


def test_manifest_round_trip_and_no_absolute_paths(archive):
    sources, manifest, _, _ = archive
    data = manifest.to_bytes()
    assert Manifest.from_bytes(data) == manifest
    assert data.endswith(b"\n")
    assert "été 東京" in data.decode("utf-8")
    assert all(str(path).encode() not in data for path in sources.values())
    assert manifest.total_files == 3
    assert manifest.total_bytes == 1208
    assert hashlib.sha256(data).hexdigest() == manifest.digest


def test_export_never_overwrites(archive, tmp_path):
    _, manifest, _, _ = archive
    path = tmp_path / "reference.json"
    export_new(path, manifest.to_bytes())
    assert load_manifest(path) == manifest
    with pytest.raises(FileExistsError):
        export_new(path, b"wrong")
    assert path.read_bytes() == manifest.to_bytes()


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc/passwd",
        "../escape",
        "a/../b",
        "a/./b",
        "a//b",
        "a/",
        "C:/x",
        "a\\b",
        "//server/share",
        "CON",
        "con.txt",
        "CON .txt",
        "LPT1.log",
        "COM¹",
        "a/NUL",
        "a.",
        "a ",
        "x:y",
        "foo\0bar",
        "foo\nbar",
        "a/*",
        "\udcff",
        "a" * 256,
    ],
)
def test_reject_unsafe_names(path):
    with pytest.raises(UnsafePath):
        portable_path(path)


@pytest.mark.parametrize(
    "paths",
    [
        ["Photo/a", "photo/b"],
        ["a.jpg", "A.JPG"],
        ["é/a", "e\u0301/b"],
        ["a", "a/b"],
        ["a", "a"],
    ],
)
def test_namespace_conflicts(paths):
    with pytest.raises(UnsafePath):
        check_namespace(paths)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(format_version=2),
        lambda d: d.update(generation=True),
        lambda d: d.update(archive_id="not-a-uuid"),
        lambda d: d.update(total_bytes=1),
        lambda d: d.update(total_files=False),
        lambda d: d.update(created_utc="2026-01-01"),
        lambda d: d.update(roots="Photos"),
        lambda d: d.update(files=None),
        lambda d: d.update(extra="unknown"),
        lambda d: d["files"][0].update(path="../../escape"),
        lambda d: d["files"][0].update(size=-1),
        lambda d: d["files"][0].update(sha256="0"),
        lambda d: d["files"][0].update(logical_path="Unknown/x"),
        lambda d: d["files"][0].update(revision=999),
    ],
)
def test_strict_loading(archive, mutate):
    _, manifest, _, _ = archive
    value = json.loads(manifest.to_bytes())
    mutate(value)
    with pytest.raises(ColdVaultError):
        Manifest.from_bytes(json.dumps(value).encode())


def test_duplicate_json_keys_rejected():
    with pytest.raises(ColdVaultError):
        Manifest.from_bytes(b'{"files": [], "files": []}')


def test_empty_archive(tmp_path):
    manifest = create_manifest({"Empty": tmp_path}, "Vide")
    assert manifest.total_files == 0
    assert manifest.total_bytes == 0


def test_empty_hash(archive):
    sources, _, _, _ = archive
    with Tree(sources["Photos"]) as tree:
        digest, size, _ = hash_file(tree, "vide")
    assert size == 0
    assert digest == hashlib.sha256(b"").hexdigest()


def test_manifest_path_generation_consistency(archive):
    _, manifest, _, _ = archive
    entry = replace(manifest.files[0], path="data/000002/Documents/papier.txt")
    with pytest.raises(ColdVaultError):
        replace(manifest, files=(entry, *manifest.files[1:])).validate()


def test_case_conflict_between_roots(archive):
    sources, _, _, _ = archive
    with pytest.raises(UnsafePath):
        create_manifest({"Photos": sources["Photos"], "photos": sources["Documents"]}, "X")
