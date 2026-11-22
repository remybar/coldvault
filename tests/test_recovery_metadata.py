import json
from dataclasses import replace

import pytest

from coldvault.control import Control
from coldvault.engine import copy_archive, create_manifest, plan_copy
from coldvault.errors import Cancelled, ColdVaultError, Conflict, UnsafePath
from coldvault.io import Tree
from coldvault.manifest import Manifest


def test_generation_must_retain_historical_entries(archive):
    sources, previous, destination, media_id = archive
    copy_archive(previous, sources, destination, media_id)
    candidate = create_manifest(sources, previous.name, previous=previous)
    removed = candidate.files[0]
    malicious = replace(
        candidate,
        files=candidate.files[1:],
        total_files=candidate.total_files - 1,
        total_bytes=candidate.total_bytes - removed.size,
    )
    malicious.validate()  # Structure valide, mais filiation sémantiquement invalide.
    with pytest.raises(Conflict, match="historiques"):
        plan_copy(malicious, sources, destination, media_id)
    assert (destination / removed.path).exists()


def test_recovery_cannot_move_an_archived_file(archive):
    sources, manifest, destination, media_id = archive
    copy_archive(manifest, sources, destination, media_id)
    original = manifest.files[0]
    # Simule un journal altéré qui désigne un fichier définitif comme temporaire.
    operation = "cbe47b74-0cee-4d21-a666-c0aa608eeae9"
    job = {
        "operation_id": operation,
        "archive_id": manifest.archive_id,
        "manifest_sha256": manifest.digest,
        "media_id": media_id,
        "temporaries": {original.path: original.path},
    }
    with Tree(destination) as tree:
        tree.write_new(f".coldvault/operations/{operation}.start.json", json.dumps(job).encode())
    before = (destination / original.path).read_bytes()
    with pytest.raises(UnsafePath):
        copy_archive(manifest, sources, destination, media_id, resume=True)
    assert (destination / original.path).read_bytes() == before


def test_recovery_requires_the_original_manifest(archive):
    sources, manifest, destination, media_id = archive
    control = Control(lambda event: control.cancel() if event.phase == "copie" else None)
    with pytest.raises(Cancelled):
        copy_archive(manifest, sources, destination, media_id, control=control)
    different = replace(manifest, name="Autre référence")
    with pytest.raises(Conflict, match="Journal incomplet"):
        copy_archive(different, sources, destination, media_id, resume=True)


def test_metadata_are_reread_before_publication(tmp_path, monkeypatch):
    original = Tree.read

    def corrupt(tree, relative):
        data = original(tree, relative)
        return b"corrupted" if relative.endswith(".part") else data

    monkeypatch.setattr(Tree, "read", corrupt)
    with Tree(tmp_path) as tree, pytest.raises(ColdVaultError, match="temporaires non conformes"):
        tree.write_new("manifest.json", b'{"value": 1}')
    assert not (tmp_path / "manifest.json").exists()


def test_serialization_digest_survives_ascii_and_crlf_encodings(archive):
    _, manifest, _, _ = archive
    foreign = json.dumps(json.loads(manifest.to_bytes()), ensure_ascii=True, indent=4)
    foreign = foreign.replace("\n", "\r\n").encode("utf-8")
    loaded = Manifest.from_bytes(foreign)
    assert loaded.to_bytes() == manifest.to_bytes()
    assert loaded.digest == manifest.digest


def test_replaced_destination_root_is_detected(archive):
    sources, manifest, destination, media_id = archive
    switched = False
    moved = destination.with_name("HDD-A-moved")

    def swap(event):
        nonlocal switched
        if event.phase == "copie" and not switched:
            # Windows peut refuser le renommage d'un dossier ouvert : simulation POSIX seulement.
            destination.rename(moved)
            destination.mkdir()
            switched = True

    import os

    if os.name != "posix":
        pytest.skip("Changement de racine ouverte POSIX")
    with pytest.raises(UnsafePath, match="racine"):
        copy_archive(manifest, sources, destination, media_id, control=Control(swap))
    assert not list(destination.iterdir())
    assert not list(moved.glob(".coldvault/manifests/*.json"))
