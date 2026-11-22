import json
import subprocess
import sys
from pathlib import Path

from coldvault.cli import main
from coldvault.engine import copy_archive, export_new


def test_cli_full_lifecycle(tmp_path, capsys):
    source = tmp_path / "source"
    source.mkdir()
    (source / "é.txt").write_bytes(b"hello")
    manifest = tmp_path / "g1.json"
    assert (
        main(
            [
                "create",
                "--name",
                "Famille",
                "--source",
                f"Documents={source}",
                "--output",
                str(manifest),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "prepared_not_copied"
    target = tmp_path / "HDD"
    target.mkdir()
    assert (
        main(
            [
                "media-init",
                "--manifest",
                str(manifest),
                "--destination",
                str(target),
                "--label",
                "HDD-A",
            ]
        )
        == 0
    )
    media_id = json.loads(capsys.readouterr().out)["media_id"]
    command = [
        "copy",
        "--manifest",
        str(manifest),
        "--source",
        f"Documents={source}",
        "--destination",
        str(target),
        "--media-id",
        media_id,
    ]
    assert main(command) == 0
    assert not (target / "data").exists()
    capsys.readouterr()
    assert main([*command, "--execute"]) == 0
    assert "verified_immediate_reconnection_pending" in capsys.readouterr().out
    report = tmp_path / "report.json"
    assert (
        main(
            [
                "verify",
                "--manifest",
                str(manifest),
                "--destination",
                str(target),
                "--media-id",
                media_id,
                "--after-reconnection",
                "--record-on-media",
                "--report",
                str(report),
            ]
        )
        == 0
    )
    assert json.loads(report.read_bytes())["reconnection_evidence"] == "user_declaration"
    (source / "new").write_bytes(b"new")
    candidate = tmp_path / "g2.json"
    capsys.readouterr()
    assert (
        main(
            [
                "update",
                "--manifest",
                str(manifest),
                "--source",
                f"Documents={source}",
                "--output",
                str(candidate),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["new"] == ["Documents/new"]
    assert main([*command[:2], str(candidate), *command[3:], "--execute"]) == 0


def test_export_inside_source_rejected(tmp_path, capsys):
    assert (
        main(
            [
                "create",
                "--name",
                "X",
                "--source",
                f"Photos={tmp_path}",
                "--output",
                str(tmp_path / "manifest.json"),
            ]
        )
        == 2
    )
    assert not (tmp_path / "manifest.json").exists()


def test_cli_verification_nonzero_when_corrupt(archive, tmp_path, capsys):
    sources, manifest, target, media_id = archive
    copy_archive(manifest, sources, target, media_id)
    manifest_file = tmp_path / "manifest.json"
    export_new(manifest_file, manifest.to_bytes())
    (target / manifest.files[0].path).write_bytes(b"wrong")
    assert (
        main(
            [
                "verify",
                "--manifest",
                str(manifest_file),
                "--destination",
                str(target),
                "--report",
                str(tmp_path / "report.json"),
            ]
        )
        == 1
    )


def test_independent_reader_without_package(archive, tmp_path):
    sources, manifest, target, media_id = archive
    copy_archive(manifest, sources, target, media_id)
    reference = tmp_path / "reference.json"
    reference.write_bytes(manifest.to_bytes())
    script = Path(__file__).resolve().parents[1] / "tools/verify_manifest.py"
    command = [sys.executable, "-I", str(script), str(reference), str(target)]
    good = subprocess.run(command, capture_output=True, text=True, check=False)
    assert good.returncode == 0, good.stderr
    assert good.stdout.count('"verified"') == manifest.total_files
    (target / manifest.files[0].path).write_bytes(b"wrong")
    bad = subprocess.run(command, capture_output=True, text=True, check=False)
    assert bad.returncode == 1
