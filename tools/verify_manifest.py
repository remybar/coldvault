#!/usr/bin/env python3
"""Lecteur de secours autonome. Bibliothèque standard, aucune écriture.

Usage : python3 verify_manifest.py manifeste.json dossier_archive
Utiliser un manifeste de confiance, sur un arbre au repos.
"""

import hashlib
import json
import os
import stat
import sys
from pathlib import Path


def regular_path(root, relative):
    if (
        not isinstance(relative, str)
        or "\\" in relative
        or ":" in relative
        or any(c in ("", ".", "..") for c in relative.split("/"))
    ):
        raise ValueError("Chemin relatif dangereux")
    current = root
    for part in relative.split("/"):
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Lien ou jonction refusé")
    if not stat.S_ISREG(current.stat().st_mode):
        raise ValueError("Fichier non régulier")
    return current


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("Usage : verify_manifest.py manifeste.json dossier_archive", file=sys.stderr)
        return 2
    try:
        manifest = json.loads(Path(args[0]).read_text(encoding="utf-8"))
        if manifest["format_version"] != 1:
            raise ValueError("Version de manifeste inconnue")
        selected = Path(args[1]).absolute()
        info = selected.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Racine liée refusée")
        root = selected.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Un dossier d'archive est requis")
        expected = set()
        failed = False
        total = 0
        for entry in manifest["files"]:
            relative = entry["path"]
            status = "verified"
            if relative in expected:
                raise ValueError("Chemin dupliqué dans le manifeste")
            expected.add(relative)
            total += entry["size"]
            try:
                path = regular_path(root, relative)
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    before = os.fstat(stream.fileno())
                    count = 0
                    while chunk := stream.read(1024 * 1024):
                        count += len(chunk)
                        digest.update(chunk)
                    after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    status = "changed_during_read"
                elif count != entry["size"]:
                    status = "size_mismatch"
                elif digest.hexdigest() != entry["sha256"]:
                    status = "hash_mismatch"
            except (OSError, ValueError) as exc:
                status = f"error: {exc}"
            failed |= status != "verified"
            print(json.dumps({"path": relative, "status": status}, ensure_ascii=True))

        def walk_error(exc):
            raise exc

        for directory, folders, files in os.walk(root, followlinks=False, onerror=walk_error):
            if Path(directory) == root and ".coldvault" in folders:
                folders.remove(".coldvault")
            for name in folders + files:
                path = Path(directory) / name
                info = path.lstat()
                linked = (
                    stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                )
                relative = path.relative_to(root).as_posix()
                if linked or (name in files and relative not in expected):
                    failed = True
                    print(json.dumps({"path": relative, "status": "link" if linked else "extra"}))
        if manifest["total_files"] != len(expected) or manifest["total_bytes"] != total:
            raise ValueError("Totaux du manifeste incohérents")
        return 1 if failed else 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
