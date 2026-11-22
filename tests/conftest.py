from pathlib import Path

import pytest

from coldvault.engine import create_manifest, initialize_media


@pytest.fixture
def archive(tmp_path: Path):
    photos = tmp_path / "sources" / "photos"
    docs = tmp_path / "sources" / "documents"
    photos.mkdir(parents=True)
    docs.mkdir(parents=True)
    (photos / "été 東京.jpg").write_bytes(b"photographie" * 100)
    (photos / "vide").write_bytes(b"")
    (docs / "papier.txt").write_bytes(b"Contrat\n")
    sources = {"Photos": photos, "Documents": docs}
    manifest = create_manifest(sources, "Famille")
    destination = tmp_path / "HDD-A"
    destination.mkdir()
    media = initialize_media(destination, manifest, "Chez les beaux-parents")
    return sources, manifest, destination, media["media_id"]
