"""Interface Qt française ; les opérations disque passent toutes par JobDialog."""

import json
import os
import sys
import time
import uuid
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QStandardPaths,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

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
from .errors import Cancelled, ColdVaultError, UnsafePath
from .manifest import json_bytes, utc_now


def volume(size: int) -> str:
    for unit in ("o", "Kio", "Mio", "Gio", "Tio"):
        if size < 1024 or unit == "Tio":
            return f"{size:.1f} {unit}"
        size /= 1024
    return str(size)


def plain(text: str) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class Catalog:
    """Cache local seulement. Aucune décision d'intégrité ne dépend de ce fichier."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "catalog.json"

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ColdVaultError("Catalogue local invalide ; les archives restent indépendantes")
        return value

    def save(self, records: list[dict]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / f"cache-{uuid.uuid4().hex}.tmp"
        with temporary.open("xb") as stream:
            stream.write(json_bytes(records))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)  # Cache local modifiable, jamais du contenu archivé.


class Worker(QThread):
    progress = Signal(object)

    def __init__(self, job, parent=None):
        super().__init__(parent)
        self.job = job
        self.value = None
        self.error = None
        self.last_progress = 0.0
        self.control = Control(self.emit_progress)

    def emit_progress(self, event: Progress):
        now = time.monotonic()
        if now - self.last_progress >= 0.1:
            self.progress.emit(event)
            self.last_progress = now

    def run(self):
        try:
            self.value = self.job(self.control)
        except Exception as exc:
            self.error = exc.with_traceback(None)


class JobDialog(QDialog):
    """Boucle modale Qt active ; aucun hash/copie dans le thread graphique."""

    def __init__(self, parent, title, job):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(600, 240)
        layout = QVBoxLayout(self)
        self.message = plain("Préparation en cours…")
        self.global_bar = QProgressBar()
        self.global_bar.setRange(0, 0)
        self.file_bar = QProgressBar()
        self.file_bar.setRange(0, 1000)
        self.stats = plain("Une E/S système peut retarder une pause ou une annulation.")
        for widget in (self.message, self.global_bar, self.file_bar, self.stats):
            layout.addWidget(widget)
        buttons = QHBoxLayout()
        self.pause = QPushButton("Pause")
        self.pause.setCheckable(True)
        self.pause.toggled.connect(self.toggle_pause)
        cancel = QPushButton("Annuler proprement")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(self.pause)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        self.worker = Worker(job, self)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished.connect(self.on_finished)

    def execute(self):
        self.worker.start()
        self.exec()
        self.worker.wait()
        self.worker.progress.disconnect(self.on_progress)
        self.worker.finished.disconnect(self.on_finished)
        error, value = self.worker.error, self.worker.value
        # Rompt les cycles traceback → dialogue → worker. La destruction native est
        # programmée sur le thread Qt, jamais laissée au GC pendant un signal suivant.
        self.worker.error = None
        self.worker.job = None
        self.worker.control.callback = None
        self.deleteLater()
        if error:
            raise error.with_traceback(None)
        return value

    @Slot(bool)
    def toggle_pause(self, paused):
        if paused:
            self.worker.control.pause()
            self.pause.setText("Reprendre")
        else:
            self.worker.control.resume()
            self.pause.setText("Pause")

    @Slot(object)
    def on_progress(self, event):
        self.message.setText(f"{event.phase} · {event.path}")
        self.global_bar.setRange(0, 1000)
        self.global_bar.setValue(int(1000 * event.completed_bytes / max(event.total_bytes, 1)))
        self.file_bar.setValue(int(1000 * event.file_bytes / max(event.file_size, 1)))
        eta = f"{event.remaining_seconds:.0f} s" if event.remaining_seconds is not None else "…"
        self.stats.setText(
            f"{volume(event.completed_bytes)} / {volume(event.total_bytes)} · "
            f"{volume(event.bytes_per_second)}/s · restant estimé : {eta}"
        )

    @Slot()
    def on_finished(self):
        self.accept()

    def reject(self):
        if self.worker.isRunning():
            self.worker.control.cancel()
            self.message.setText("Annulation demandée ; attente de la fin de l'E/S en cours…")
            self.pause.setEnabled(False)
        else:
            super().reject()

    def closeEvent(self, event):
        if self.worker.isRunning():
            self.reject()
            event.ignore()
        else:
            event.accept()


class SourcesPage(QWizardPage):
    def __init__(self, sources=None, *, optional=False):
        super().__init__()
        self.setTitle("Dossiers sources")
        self.setSubTitle(
            "Chaque dossier reçoit un nom logique stable, conservé dans les manifestes."
        )
        self.optional = optional
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Nom logique", "Dossier source"])
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        add = QPushButton("Ajouter un dossier…")
        add.clicked.connect(self.add_folder)
        remove = QPushButton("Retirer la sélection")
        remove.clicked.connect(self.remove_rows)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        layout.addLayout(buttons)
        layout.addWidget(
            plain(
                "Aucune écriture dans ces dossiers. Fermez les applications qui "
                "pourraient modifier les fichiers pendant l'opération."
            )
        )
        self.table.itemChanged.connect(lambda *_: self.completeChanged.emit())
        for alias, path in (sources or {}).items():
            self.add_row(alias, path)

    def add_row(self, alias, path):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(alias))
        self.table.setItem(row, 1, QTableWidgetItem(str(path)))
        self.completeChanged.emit()

    def add_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Dossier source")
        if not path:
            return
        alias, ok = QInputDialog.getText(
            self, "Nom logique", "Nom stable de cette source :", text=Path(path).name
        )
        if ok and alias.strip():
            self.add_row(alias.strip(), path)

    def remove_rows(self):
        for row in sorted({item.row() for item in self.table.selectedItems()}, reverse=True):
            self.table.removeRow(row)
        self.completeChanged.emit()

    def values(self):
        pairs = [
            (self.table.item(i, 0).text().strip(), self.table.item(i, 1).text())
            for i in range(self.table.rowCount())
        ]
        if len({alias for alias, _ in pairs}) != len(pairs):
            raise ColdVaultError("Les noms logiques doivent être uniques")
        return dict(pairs)

    def isComplete(self):
        return (self.optional or self.table.rowCount() > 0) and all(
            self.table.item(i, j) is not None and bool(self.table.item(i, j).text().strip())
            for i in range(self.table.rowCount())
            for j in (0, 1)
        )


class PrepareWizard(QWizard):
    def __init__(self, parent, record=None, *, copy_only=False):
        super().__init__(parent)
        self.setWindowTitle("Sources de la copie" if copy_only else "Préparer une génération")
        self.resize(720, 440)
        self.setButtonText(QWizard.WizardButton.NextButton, "Suivant")
        self.setButtonText(QWizard.WizardButton.BackButton, "Précédent")
        self.setButtonText(QWizard.WizardButton.CancelButton, "Annuler")
        self.setButtonText(
            QWizard.WizardButton.FinishButton, "Préparer" if not copy_only else "Continuer"
        )
        self.sources = SourcesPage((record or {}).get("sources"), optional=copy_only)
        self.addPage(self.sources)
        self.name = QLineEdit((record or {}).get("name", "Famille"))
        self.output = QLineEdit()
        if not copy_only:
            page = QWizardPage()
            page.setTitle("Manifeste indépendant")
            page.setSubTitle("Cette étape prépare la référence ; aucun HDD n'est encore validé.")
            layout = QFormLayout(page)
            layout.addRow("Nom de l'archive", self.name)
            choose = QPushButton("Choisir un nouveau fichier…")
            choose.clicked.connect(self.choose_output)
            layout.addRow("Exporter le manifeste", self.output)
            layout.addRow(choose)
            page.registerField("name*", self.name)
            page.registerField("output*", self.output)
            self.addPage(page)

    def choose_output(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Nouveau manifeste JSON", filter="JSON (*.json)"
        )
        if path:
            self.output.setText(path)


class MediaDialog(QDialog):
    def __init__(self, parent, record, *, verifying=False):
        super().__init__(parent)
        self.setWindowTitle("Vérifier un disque" if verifying else "Choisir le support")
        self.resize(660, 300)
        self.media = record.get("media", [])
        layout = QFormLayout(self)
        self.choice = QComboBox()
        self.choice.addItem(
            "Autre support, lecture seule" if verifying else "Initialiser un support vide"
        )
        for item in self.media:
            self.choice.addItem(f"{item['label']} · {item['media_id']}")
        self.destination = QLineEdit()
        self.label = QLineEdit("Nouvelle copie")
        self.expected = QLineEdit()
        self.expected.setReadOnly(True)
        browse = QPushButton("Choisir le dossier d'archive sur le disque…")
        browse.clicked.connect(self.browse)
        layout.addRow("Support connu", self.choice)
        layout.addRow("Dossier d'archive", self.destination)
        layout.addRow(browse)
        layout.addRow("Nom du support", self.label)
        layout.addRow("Identifiant attendu", self.expected)
        self.reconnected = QCheckBox(
            "J'ai éjecté, débranché puis rebranché ce disque avant ce contrôle"
        )
        self.record_report = QCheckBox("Enregistrer également le rapport sur le support identifié")
        if verifying:
            layout.addRow(self.reconnected)
            layout.addRow(self.record_report)
        else:
            layout.addRow(
                plain(
                    "Un nouveau support exige un dossier vide dédié. L'identifiant est "
                    "logique : le nom du volume ou sa lettre ne suffit pas."
                )
            )
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self.choice.currentIndexChanged.connect(self.select_media)
        if self.media:
            self.choice.setCurrentIndex(1)

    def select_media(self, index):
        item = self.media[index - 1] if index else {}
        self.destination.setText(item.get("destination", ""))
        self.label.setText(item.get("label", "Nouvelle copie"))
        self.label.setReadOnly(bool(index))
        self.expected.setText(item.get("media_id", ""))

    def browse(self):
        path = QFileDialog.getExistingDirectory(self, "Dossier d'archive dédié sur le support")
        if path:
            self.destination.setText(path)

    def accept(self):
        if not self.destination.text().strip():
            QMessageBox.warning(
                self, "Dossier requis", "Sélectionnez le dossier d'archive du disque."
            )
            return
        super().accept()


class PlanDialog(QDialog):
    def __init__(self, parent, plan):
        super().__init__(parent)
        self.setWindowTitle("Prévisualisation de la copie")
        self.resize(780, 480)
        layout = QVBoxLayout(self)
        layout.addWidget(
            plain(
                f"{plan['total_files']} fichiers · {volume(plan['total_bytes'])} au total\n"
                f"À copier : {volume(plan['copy_bytes'])} · "
                f"Libre : {volume(plan['free_bytes'])} · "
                f"Marge : {volume(plan['reserve_bytes'])}\n"
                f"Support attendu : {plan['media_id']}"
            )
        )
        table = QTableWidget(len(plan["actions"]), 2)
        table.setHorizontalHeaderLabels(["Fichier", "Opération / anomalie"])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        labels = {
            "copy": "Copier",
            "reuse_after_hash": "Relu, conforme — conserver",
            "source_unavailable": "Source inaccessible ou version historique absente",
            "source_changed": "Source différente de la référence",
            "hash_mismatch": "SHA-256 différent",
            "size_mismatch": "Taille différente",
            "io_error": "Erreur de lecture",
        }
        for row, action in enumerate(plan["actions"]):
            table.setItem(row, 0, QTableWidgetItem(action["path"]))
            table.setItem(row, 1, QTableWidgetItem(labels.get(action["action"], action["action"])))
        layout.addWidget(table)
        self.resume = QCheckBox(
            "Reprendre les opérations interrompues et conserver leurs temporaires"
        )
        self.resume.setVisible(bool(plan["incomplete_operations"]))
        layout.addWidget(self.resume)
        if plan["unexpected_files"]:
            layout.addWidget(
                plain("Fichiers supplémentaires :\n" + "\n".join(plan["unexpected_files"]))
            )
        layout.addWidget(
            plain(
                "Aucun existant ne sera écrasé. Le moteur refera les contrôles avant "
                "d'écrire. Une copie conforme devra encore être vérifiée après reconnexion."
            )
        )
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.execute_button = buttons.addButton(
            "Copier et vérifier", QDialogButtonBox.ButtonRole.AcceptRole
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        known_temps = {
            p for job in plan["incomplete_operations"] for p in job.get("temporaries", {}).values()
        }
        allowed = (
            plan["space_ok"]
            and not (set(plan["unexpected_files"]) - known_temps)
            and all(a["action"] in {"copy", "reuse_after_hash"} for a in plan["actions"])
        )
        self.execute_button.setEnabled(allowed and not plan["incomplete_operations"])
        self.resume.toggled.connect(
            lambda checked: self.execute_button.setEnabled(allowed and checked)
        )


class Window(QMainWindow):
    def __init__(self, catalog: Catalog):
        super().__init__()
        self.catalog = catalog
        self.records = []
        self.setWindowTitle("ColdVault — Mes archives")
        self.resize(920, 620)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        title = plain("ColdVault · Mes archives")
        title.setStyleSheet("font-size: 24px; font-weight: 600; padding: 12px 0;")
        layout.addWidget(title)
        layout.addWidget(plain("Archives portables · SHA-256 · Plusieurs copies indépendantes"))
        self.list = QListWidget()
        self.list.setSpacing(8)
        self.list.currentRowChanged.connect(self.show_details)
        layout.addWidget(self.list, 2)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details, 1)
        buttons = QHBoxLayout()
        for index, (label, action) in enumerate(
            (
                ("Créer une archive", lambda: self.prepare()),
                ("Ouvrir un manifeste", self.import_manifest),
                ("Copier sur un disque", self.copy),
                ("Vérifier un disque", self.check),
                ("Mettre à jour", lambda: self.prepare(update=True)),
                ("Exporter", self.export),
            )
        ):
            if index == 3:
                layout.addLayout(buttons)
                buttons = QHBoxLayout()
            button = QPushButton(label)
            button.clicked.connect(action)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.statusBar().showMessage(
            "Les états affichés sont ceux des derniers rapports, sans contrôle actuel."
        )

    def task(self, title, job):
        try:
            return JobDialog(self, title, job).execute()
        except Cancelled:
            QMessageBox.information(
                self,
                "Opération interrompue",
                "Aucune validation déduite. "
                "Une copie interrompue peut être reprise après examen du plan.",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Opération non terminée", str(exc))
        return None

    def startup(self):
        records = self.task("Charger le catalogue local", lambda _: self.catalog.load())
        if records is not None:
            self.records = records
            self.refresh()

    def selected(self):
        row = self.list.currentRow()
        if row < 0:
            QMessageBox.information(self, "Choisir une archive", "Créez ou ouvrez un manifeste.")
            return None
        return self.records[row]

    def remember(self, manifest, path, sources=None, media=None):
        # Appel depuis le worker uniquement : le cache est écrit hors thread Qt.
        records = json.loads(json.dumps(self.records))
        record = next((r for r in records if r["archive_id"] == manifest.archive_id), None)
        if record is None:
            record = {"archive_id": manifest.archive_id, "sources": {}, "media": []}
            records.append(record)
        record.update(
            name=manifest.name,
            generation=manifest.generation,
            manifest=str(path),
            total_files=manifest.total_files,
            total_bytes=manifest.total_bytes,
        )
        if sources is not None:
            record["sources"] = {k: str(v) for k, v in sources.items()}
        if media:
            old = next((m for m in record["media"] if m["media_id"] == media["media_id"]), None)
            if old:
                old.update(media)
            else:
                record["media"].append(media)
        self.catalog.save(records)
        return records

    def refresh(self, archive_id=None):
        self.list.clear()
        for record in self.records:
            self.list.addItem(
                f"{record['name']} · génération {record['generation']}\n"
                f"{record['total_files']} fichiers · {volume(record['total_bytes'])} · "
                f"{len(record['media'])} supports configurés"
            )
        row = next((i for i, r in enumerate(self.records) if r["archive_id"] == archive_id), 0)
        if self.records:
            self.list.setCurrentRow(row)

    def show_details(self, row):
        if row < 0:
            self.details.clear()
            return
        record = self.records[row]
        lines = [f"Manifeste : {record['manifest']}", "Historique des derniers contrôles :"]
        for item in record.get("media", []):
            lines.extend(
                [
                    f"\n{item['label']} · {item['media_id']}",
                    f"Dossier connu : {item['destination']}",
                    f"Dernier état : {item.get('status', 'Support associé, non contrôlé')}",
                    f"Dernier contrôle : {item.get('last_check', 'Aucun')}",
                    f"Dernière mise à jour : {item.get('last_update', 'Aucune')}",
                    f"Rapport : {item.get('report_path', 'Aucun')}",
                ]
            )
        lines.append("\nHistorique complet et erreurs : .coldvault/reports sur chaque support.")
        self.details.setPlainText("\n".join(lines))

    def import_manifest(self):
        path, _ = QFileDialog.getOpenFileName(self, "Ouvrir un manifeste", filter="JSON (*.json)")
        if not path:
            return
        records = self.task("Lire le manifeste", lambda _: self.remember(load_manifest(path), path))
        if records is not None:
            self.records = records
            self.refresh()

    def prepare(self, update=False):
        record = self.selected() if update else None
        if update and not record:
            return
        wizard = PrepareWizard(self, record)
        if wizard.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            sources = wizard.sources.values()
        except ColdVaultError as exc:
            QMessageBox.warning(self, "Sources invalides", str(exc))
            return
        output, name = wizard.output.text(), wizard.name.text()

        def job(control):
            ensure_export_outside_sources(output, sources)
            previous = load_manifest(record["manifest"]) if record else None
            manifest = create_manifest(sources, name, previous=previous, control=control)
            summary = {"fichiers": manifest.total_files, "volume": volume(manifest.total_bytes)}
            if previous:
                summary.update(update_summary(previous, manifest))
                summary.update(source_comparison(manifest, sources))
            export_new(output, manifest.to_bytes())
            return self.remember(manifest, output, sources), manifest.archive_id, summary

        result = self.task("Analyse et calcul des empreintes", job)
        if result:
            self.records, archive_id, summary = result
            self.refresh(archive_id)
            QMessageBox.information(
                self,
                "Référence préparée — copie à effectuer",
                "Le manifeste est exporté. Examinez les ajouts et versions, "
                "puis choisissez « Copier sur un disque ».\n\n" + self.preparation_summary(summary),
            )

    @staticmethod
    def preparation_summary(summary):
        lines = [f"{summary['fichiers']} fichiers conservés · {summary['volume']}"]
        labels = {
            "new": "Nouveaux fichiers",
            "modified": "Nouvelles versions",
            "unchanged": "Fichiers inchangés",
            "deleted_from_sources": "Absents des sources, conservés",
        }
        for key, label in labels.items():
            if key in summary:
                values = summary[key]
                lines.append(f"\n{label} : {len(values)}")
                lines.extend(values[:15])
                if len(values) > 15:
                    lines.append("… Voir le manifeste pour l'inventaire complet.")
        lines.append("\nToute nouvelle version conserve les précédentes.")
        return "\n".join(lines)

    def copy(self):
        record = self.selected()
        if not record:
            return
        wizard = PrepareWizard(self, record, copy_only=True)
        if wizard.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            sources = wizard.sources.values()
        except ColdVaultError as exc:
            QMessageBox.warning(self, "Sources invalides", str(exc))
            return
        media = MediaDialog(self, record)
        if media.exec() != QDialog.DialogCode.Accepted:
            return
        destination, label, media_id = (
            media.destination.text(),
            media.label.text(),
            media.expected.text(),
        )
        if not media_id:
            answer = QMessageBox.question(
                self,
                "Associer ce support",
                f"Initialiser le dossier vide {destination} pour "
                f"l'archive {record['name']} sous le nom {label} ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        def planning(control):
            manifest = load_manifest(record["manifest"])
            identity = media_id
            if not identity:
                target = Path(destination).resolve(strict=True)
                for source in sources.values():
                    root = Path(source).resolve(strict=True)
                    if root.is_relative_to(target) or target.is_relative_to(root):
                        raise UnsafePath("Sources et destination doivent être distinctes")
                identity = initialize_media(destination, manifest, label)["media_id"]
            plan = plan_copy(manifest, sources, destination, identity, control=control)
            records = self.remember(
                manifest,
                record["manifest"],
                sources,
                {"media_id": identity, "label": label, "destination": destination},
            )
            return manifest, identity, plan, records

        result = self.task("Analyser le support et préparer le plan", planning)
        if not result:
            return
        manifest, media_id, plan, self.records = result
        self.refresh(manifest.archive_id)
        preview = PlanDialog(self, plan)
        if preview.exec() != QDialog.DialogCode.Accepted:
            return
        resume = preview.resume.isChecked()

        def execute(control):
            report = copy_archive(
                manifest, sources, destination, media_id, resume=resume, control=control
            )
            media_record = {
                "media_id": media_id,
                "label": label,
                "destination": destination,
                "status": "Conforme immédiatement ; reconnexion à effectuer",
                "generation": manifest.generation,
                "last_check": report.finished_utc,
                "last_update": utc_now(),
                "report_path": str(
                    Path(destination) / f".coldvault/reports/{report.report_id}.json"
                ),
            }
            return self.remember(manifest, record["manifest"], sources, media_record)

        records = self.task("Copie et relecture complète", execute)
        if records is not None:
            self.records = records
            self.refresh(manifest.archive_id)
            QMessageBox.information(
                self,
                "Contrôle immédiat conforme",
                "La vérification après reconnexion reste à effectuer.\n\n"
                "1. Éjectez le disque avec le système d'exploitation.\n"
                "2. Débranchez-le puis rebranchez-le.\n"
                "3. Choisissez « Vérifier un disque » et déclarez cette reconnexion.\n\n"
                "ColdVault ne détecte pas physiquement ces gestes.",
            )

    def check(self):
        record = self.selected()
        if not record:
            return
        media = MediaDialog(self, record, verifying=True)
        if media.exec() != QDialog.DialogCode.Accepted:
            return
        output, _ = QFileDialog.getSaveFileName(
            self, "Exporter le nouveau rapport", filter="JSON (*.json)"
        )
        if not output:
            return
        destination, media_id, label = (
            media.destination.text(),
            media.expected.text(),
            media.label.text(),
        )
        reconnected, record_on_media = (
            media.reconnected.isChecked(),
            media.record_report.isChecked(),
        )

        def job(control):
            manifest = load_manifest(record["manifest"])
            ensure_export_outside_sources(output, record.get("sources", {}))
            # Le rapport indépendant ne doit pas être placé dans le contenu à vérifier.
            if Path(output).absolute().parent.resolve().is_relative_to(Path(destination).resolve()):
                raise UnsafePath("Choisir un dossier de rapport indépendant du support")
            report = verify(
                manifest,
                destination,
                expected_media_id=media_id or None,
                control=control,
                context="after_reconnection" if reconnected else "standalone",
                user_declared_reconnected=reconnected,
            )
            export_new(output, report.to_bytes())
            if record_on_media:
                if not report.media_id:
                    raise ColdVaultError(
                        "Le rapport est exporté ; ce support n'a pas d'identité ColdVault"
                    )
                record_report(manifest, destination, report.media_id, report)
            status = (
                "Conforme après reconnexion déclarée"
                if reconnected
                else "Contrôle complet conforme"
            )
            if not report.ok:
                status = "Non conforme ou contrôle interrompu — voir le rapport"
            media_record = (
                {
                    "media_id": report.media_id,
                    "label": label,
                    "destination": destination,
                    "status": status,
                    "generation": manifest.generation,
                    "last_check": report.finished_utc,
                    "report_path": output,
                }
                if report.media_id
                else None
            )
            return self.remember(manifest, record["manifest"], media=media_record), report

        result = self.task("Vérification SHA-256 complète", job)
        if result:
            self.records, report = result
            self.refresh(record["archive_id"])
            summary = {}
            for item in report.results:
                summary[item.status] = summary.get(item.status, 0) + 1
            labels = {
                "verified": "Vérifiés",
                "missing": "Manquants",
                "size_mismatch": "Taille différente",
                "hash_mismatch": "Empreinte différente",
                "io_error": "Erreur de lecture",
                "extra": "Supplémentaires",
                "temporary": "Temporaires résiduels",
                "not_verified": "Non vérifiés",
            }
            QMessageBox.information(
                self,
                "Contrôle conforme" if report.ok else "Contrôle non conforme",
                f"Rapport exporté : {output}\n\n"
                + "\n".join(f"{labels.get(key, key)} : {count}" for key, count in summary.items()),
            )

    def export(self):
        record = self.selected()
        if not record:
            return
        destination = QFileDialog.getExistingDirectory(self, "Dossier indépendant d'export")
        if not destination:
            return

        def job(_):
            target = Path(destination).resolve(strict=True)
            roots = list(record.get("sources", {}).values()) + [
                m["destination"] for m in record["media"]
            ]
            if any(target.is_relative_to(Path(root).resolve()) for root in roots):
                raise UnsafePath("Choisir un dossier indépendant des sources et archives")
            manifest = load_manifest(record["manifest"])
            export_new(target / manifest.filename, manifest.to_bytes())
            count = 1
            for media in record["media"]:
                if media.get("report_path"):
                    path = Path(media["report_path"])
                    export_new(
                        target / f"rapport-{media['media_id']}-{path.name}", path.read_bytes()
                    )
                    count += 1
            return count

        count = self.task("Exporter le manifeste et les derniers rapports", job)
        if count:
            QMessageBox.information(
                self, "Export terminé", f"{count} fichiers exportés dans {destination}."
            )


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ColdVault")
    app.setOrganizationName("ColdVault")
    directory = Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    )
    window = Window(Catalog(directory))
    window.show()
    if "--smoke-test" in sys.argv:
        # Contrôle du bundle, sans lecture du profil ni accès à un support utilisateur.
        QTimer.singleShot(100, app.quit)
    else:
        window.startup()
    result = app.exec()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
