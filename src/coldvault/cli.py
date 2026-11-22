"""Interface française. La copie reste un plan tant que --execute est absent."""

import argparse
import json
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .control import Control, Progress
from .engine import (
    copy_archive,
    create_manifest,
    ensure_export_outside_sources,
    export_new,
    initialize_media,
    load_manifest,
    plan_copy,
    record_report,
    source_comparison,
    update_summary,
    verify,
)
from .errors import Cancelled, ColdVaultError


def sources_from(values: list[str]) -> dict[str, Path]:
    result = {}
    for item in values:
        alias, separator, path = item.partition("=")
        if not separator or not path or alias in result:
            raise ColdVaultError("Utiliser --source ALIAS=CHEMIN, avec des alias uniques")
        result[alias] = Path(path)
    return result


def parser() -> argparse.ArgumentParser:
    app = argparse.ArgumentParser(description="ColdVault — archives froides vérifiables")
    sub = app.add_subparsers(dest="command", required=True)
    for verb, help_text in (
        ("create", "Préparer une première génération"),
        ("update", "Préparer une génération additive"),
    ):
        cmd = sub.add_parser(verb, help=help_text)
        cmd.add_argument("--source", action="append", required=True, metavar="ALIAS=CHEMIN")
        cmd.add_argument("--output", required=True, help="Nouveau fichier manifeste")
        if verb == "create":
            cmd.add_argument("--name", required=True)
        else:
            cmd.add_argument("--manifest", required=True, help="Dernière génération contrôlée")
    init = sub.add_parser("media-init", help="Associer explicitement un dossier vide à un support")
    init.add_argument("--manifest", required=True)
    init.add_argument("--destination", required=True)
    init.add_argument("--label", required=True)
    copy = sub.add_parser("copy", help="Afficher un plan, ou copier avec --execute")
    copy.add_argument("--manifest", required=True)
    copy.add_argument("--source", action="append", default=[], metavar="ALIAS=CHEMIN")
    copy.add_argument("--destination", required=True)
    copy.add_argument("--media-id", required=True)
    copy.add_argument("--execute", action="store_true")
    copy.add_argument("--resume", action="store_true")
    check = sub.add_parser("verify", help="Relire tous les fichiers, sans les sources")
    check.add_argument("--manifest", required=True)
    check.add_argument("--destination", required=True)
    check.add_argument("--media-id")
    check.add_argument("--report", required=True, help="Nouveau rapport JSON indépendant")
    check.add_argument(
        "--after-reconnection",
        action="store_true",
        help="Déclaration utilisateur : disque éjecté puis rebranché",
    )
    check.add_argument(
        "--record-on-media",
        action="store_true",
        help="Enregistrer aussi le contrôle sur le support identifié",
    )
    return app


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    last_update = 0.0

    def progress(event: Progress) -> None:
        nonlocal last_update
        now = time.monotonic()
        if now - last_update < 0.25:
            return
        last_update = now
        eta = f"{event.remaining_seconds:.0f}s" if event.remaining_seconds is not None else "?"
        # JSON sur stderr : noms de fichiers échappés, aucun contenu de fichier journalisé.
        print(json.dumps({**asdict(event), "eta": eta}, ensure_ascii=True), file=sys.stderr)

    control = Control(progress)
    previous_handler = signal.signal(signal.SIGINT, lambda *_: control.cancel())
    try:
        if args.command in {"create", "update"}:
            sources = sources_from(args.source)
            ensure_export_outside_sources(args.output, sources)
            previous = load_manifest(args.manifest) if args.command == "update" else None
            manifest = create_manifest(
                sources,
                previous.name if previous else args.name,
                previous=previous,
                control=control,
            )
            export_new(args.output, manifest.to_bytes())
            summary = {
                "manifest": str(args.output),
                "sha256": manifest.digest,
                "total_files": manifest.total_files,
                "total_bytes": manifest.total_bytes,
                "status": "prepared_not_copied",
            }
            if previous:
                summary.update(update_summary(previous, manifest))
                summary.update(source_comparison(manifest, sources))
            print(json.dumps(summary, ensure_ascii=True, indent=2))
        elif args.command == "media-init":
            manifest = load_manifest(args.manifest)
            print(json.dumps(initialize_media(args.destination, manifest, args.label), indent=2))
        elif args.command == "copy":
            manifest = load_manifest(args.manifest)
            sources = sources_from(args.source)
            plan = plan_copy(manifest, sources, args.destination, args.media_id, control=control)
            print(json.dumps(plan, ensure_ascii=True, indent=2), flush=True)
            if args.execute:
                report = copy_archive(
                    manifest,
                    sources,
                    args.destination,
                    args.media_id,
                    resume=args.resume,
                    control=control,
                )
                print(
                    json.dumps(
                        {
                            "report_id": report.report_id,
                            "ok": report.ok,
                            "status": "verified_immediate_reconnection_pending",
                        },
                        indent=2,
                    )
                )
        elif args.command == "verify":
            if args.record_on_media and not args.media_id:
                raise ColdVaultError("--record-on-media exige --media-id")
            manifest = load_manifest(args.manifest)
            report = verify(
                manifest,
                args.destination,
                control=control,
                expected_media_id=args.media_id,
                context="after_reconnection" if args.after_reconnection else "standalone",
                user_declared_reconnected=args.after_reconnection,
            )
            export_new(args.report, report.to_bytes())
            if args.record_on_media:
                record_report(manifest, args.destination, args.media_id, report)
            print(report.to_bytes().decode("utf-8"), end="")
            if any(r.status == "not_verified" for r in report.results):
                return 130
            return 0 if report.ok else 1
        return 0
    except Cancelled as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 130
    except (ColdVaultError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"ok": False, "error_type": type(exc).__name__, "error": str(exc)},
                ensure_ascii=True,
            ),
            file=sys.stderr,
        )
        return 2
    finally:
        signal.signal(signal.SIGINT, previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())
