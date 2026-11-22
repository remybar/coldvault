import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from coldvault.gui import Catalog, JobDialog, PlanDialog, PrepareWizard, Window  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def release_widgets_before_application(app):
    yield
    for widget in app.topLevelWidgets():
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_job_runs_off_main_thread_and_propagates_result(app):
    main_thread = threading.get_ident()
    dialog = JobDialog(None, "Test", lambda control: (threading.get_ident(), 42))
    worker_thread, result = dialog.execute()
    assert worker_thread != main_thread
    assert result == 42
    assert not dialog.worker.isRunning()


def test_job_propagates_failure_without_losing_worker(app):
    def fail(control):
        raise OSError("Disque disparu")

    dialog = JobDialog(None, "Test", fail)
    with pytest.raises(OSError, match="Disque disparu"):
        dialog.execute()
    assert not dialog.worker.isRunning()


def test_failed_job_followed_by_progress(app):
    from coldvault.control import Progress

    def failed(control):
        raise OSError("Failure before next worker")

    with pytest.raises(OSError):
        JobDialog(None, "First", failed).execute()

    def following(control):
        control.callback(Progress("vérification", "photo", 10, 10, 10, 10, 1.0, 0))
        return "finished"

    assert JobDialog(None, "Second", following).execute() == "finished"


def test_application_starts_and_exits_cleanly_without_user_data():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "coldvault.gui", "--smoke-test"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_plan_blocks_unknown_extras_and_requires_resume(app):
    base = {
        "total_files": 1,
        "total_bytes": 3,
        "copy_bytes": 3,
        "free_bytes": 99999999,
        "reserve_bytes": 16 * 1024 * 1024,
        "media_id": "TEST",
        "space_ok": True,
        "actions": [{"path": "data/file", "action": "copy"}],
        "unexpected_files": [],
        "incomplete_operations": [],
    }
    assert PlanDialog(None, base).execute_button.isEnabled()
    assert not PlanDialog(None, {**base, "space_ok": False}).execute_button.isEnabled()
    assert not PlanDialog(
        None, {**base, "unexpected_files": ["unknown"]}
    ).execute_button.isEnabled()
    resumed = PlanDialog(
        None,
        {
            **base,
            "incomplete_operations": [{"temporaries": {"data/file": "temp"}}],
            "unexpected_files": ["temp"],
        },
    )
    assert not resumed.execute_button.isEnabled()
    resumed.resume.setChecked(True)
    assert resumed.execute_button.isEnabled()


def test_sources_are_editable_and_optional_for_rechecking_existing_copy(app, tmp_path):
    wizard = PrepareWizard(None)
    assert not wizard.sources.isComplete()
    wizard.sources.add_row("Photos", tmp_path)
    assert wizard.sources.isComplete()
    assert wizard.sources.values() == {"Photos": str(tmp_path)}
    assert PrepareWizard(None, copy_only=True).sources.isComplete()


def test_catalog_and_home_do_not_claim_current_integrity(app, tmp_path):
    catalog = Catalog(tmp_path)
    record = {
        "name": "Famille",
        "archive_id": "test",
        "generation": 1,
        "total_files": 3,
        "total_bytes": 10,
        "manifest": "reference.json",
        "sources": {},
        "media": [],
    }
    catalog.save([record])
    assert catalog.load() == [record]
    window = Window(catalog)
    window.records = catalog.load()
    window.refresh()
    assert window.list.count() == 1
    assert "derniers rapports" in window.statusBar().currentMessage()
    assert "reference.json" in window.details.toPlainText()
    assert QDialog.DialogCode.Accepted == 1
    window.close()


def test_graphical_lifecycle_uses_engine_workers(app, tmp_path, monkeypatch):
    from coldvault import gui

    source = tmp_path / "Photos"
    source.mkdir()
    (source / "photo.jpg").write_bytes(b"original")
    hdd = tmp_path / "HDD"
    hdd.mkdir()
    manifest_path = tmp_path / "g1.json"
    report_path = tmp_path / "report.json"
    messages, failures = [], []
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *args: messages.append(args[1:]))
    monkeypatch.setattr(gui.QMessageBox, "critical", lambda *args: failures.append(args[1:]))
    monkeypatch.setattr(
        gui.QMessageBox, "question", lambda *args: gui.QMessageBox.StandardButton.Yes
    )
    wizard_type = gui.PrepareWizard
    media_type = gui.MediaDialog
    plan_type = gui.PlanDialog

    def wizard(parent, record=None, **kwargs):
        dialog = wizard_type(parent, record, **kwargs)
        if not record:
            dialog.sources.add_row("Photos", source)
        dialog.output.setText(str(manifest_path))
        dialog.exec = lambda: QDialog.DialogCode.Accepted
        return dialog

    def medium(parent, record, **kwargs):
        dialog = media_type(parent, record, **kwargs)
        dialog.destination.setText(str(hdd))
        if kwargs.get("verifying"):
            dialog.reconnected.setChecked(True)  # Geste utilisateur simulé, pas preuve matérielle.
            dialog.record_report.setChecked(True)
        dialog.exec = lambda: QDialog.DialogCode.Accepted
        return dialog

    def preview(parent, plan):
        dialog = plan_type(parent, plan)
        assert dialog.execute_button.isEnabled()
        dialog.exec = lambda: QDialog.DialogCode.Accepted
        return dialog

    monkeypatch.setattr(gui, "PrepareWizard", wizard)
    monkeypatch.setattr(gui, "MediaDialog", medium)
    monkeypatch.setattr(gui, "PlanDialog", preview)
    monkeypatch.setattr(gui.QFileDialog, "getSaveFileName", lambda *a, **kw: (str(report_path), ""))
    window = Window(Catalog(tmp_path / "cache"))
    window.prepare()
    assert not failures
    assert manifest_path.exists()
    assert window.list.count() == 1
    window.copy()
    assert not failures
    assert len(window.records[0]["media"]) == 1
    assert "reconnexion à effectuer" in window.details.toPlainText()
    window.check()
    assert not failures
    assert report_path.exists()
    assert "Conforme après reconnexion déclarée" in window.details.toPlainText()
    (source / "new.jpg").write_bytes(b"new")
    manifest_path = tmp_path / "g2.json"
    window.prepare(update=True)
    assert not failures
    assert window.records[0]["generation"] == 2
    window.copy()
    assert not failures
    assert len(list(hdd.glob(".coldvault/manifests/*.json"))) == 2
    assert (hdd / "data/000001/Photos/photo.jpg").read_bytes() == b"original"
    assert (hdd / "data/000002/Photos/new.jpg").read_bytes() == b"new"
    assert messages
    window.close()
