"""Shareable diagnostics must be an allowlist, never a redacted raw dump."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from contextlib import ExitStack

from PyQt6 import QtCore, QtWidgets
from sshupdater import __version__
from sshupdater.docker_diagnostics import report
from sshupdater.ui_docker_diagnostics import DockerDiagnosticsDialog, PRIVACY_DE, PRIVACY_EN
from sshupdater.ui_docker_preview import DockerUpdatePreviewDialog
from sshupdater.core import docker_image_updates as images
from sshupdater.core import docker_pull, docker_apply, docker_preflight, ssh_connection

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
SECRET = 'SECRET_SENTINEL_ä'


def fixture(reason='context'):
    return ({'name': 'private.example', 'primary_ip': '192.168.9.123',
             'docker_version': 'Docker version 29.8.1, build abcdef123',
             'compose_version': 'Docker Compose version v5.5.1'},
            {'name': 'private-project', 'config_files': ['/srv/private/compose.yaml'],
             'image_updates': {'checked_at': '2026-10-14T10:00:00+00:00',
                               'images': [{'service': 'private-service', 'image': 'private.example/private:tag',
                                           'status': 'uncheckable', 'reason': reason}]}})


class ReportTests(unittest.TestCase):
    def test_existing_context_error_does_not_invent_subcause(self):
        host, project = fixture()
        text = report(host, project, exported_at=NOW)
        for expected in (__version__, '2026-10-14T12:00:00+00:00', 'Fehlerklasse: context',
                         'Kontextprüfung: fehlgeschlagen', 'Genauere Ursache im Prüfergebnis nicht erfasst.',
                         'Compose-Config-Auflösung: nicht erfasst', 'Freigabe möglich: nein'):
            self.assertIn(expected, text)
        self.assertNotIn('COMPOSE_SOURCE_FILTER', text)
        self.assertIn('Prüfphase: Docker-Prüfung (Teilschritt nicht erfasst)', text)

    def test_registry_failure_requires_phase_evidence(self):
        for reason in ('auth', 'rate_limit', 'timeout', 'network'):
            host, project = fixture(reason)
            row = project['image_updates']['images'][0]
            self.assertIn('Registry-Fehlerklasse: nicht erfasst', report(host, project))
            row['command'] = "docker buildx imagetools inspect private.example/private:tag --format '{{json .Manifest}}'"
            text = report(host, project)
            self.assertIn('Prüfphase: Registry-Abfrage', text)
            self.assertIn('Registry-Fehlerklasse: ' + reason, text)
            self.assertNotIn(row['command'], text)

    def test_missing_and_unexpected_fields(self):
        for host, project in ((None, None), ({}, {}), ([], [])):
            self.assertIn('Buildx-Version: nicht erfasst', report(host, project))
        host, project = fixture()
        for value in (None, SECRET, [], {}, 42):
            with self.subTest(value=value):
                check = {'images': [value, {'status': value, 'reason': value, 'local_descriptor': value}],
                         'compose_context': value, 'compose_identity': value, 'checked_at': value}
                project['image_updates'] = check
                self.assertNotIn(SECRET, report(host, project))

    def test_secret_sentinels_never_exported(self):
        host, project = fixture()
        host.update(name=SECRET, primary_ip=SECRET, docker_version=SECRET,
                    compose_version=SECRET, buildx_version=SECRET, connection_identity=(SECRET,))
        project.update(name=SECRET, config_files=[SECRET], config_files_raw=SECRET,
                       environment={'PASSWORD': SECRET}, config=SECRET, preflight={'note': SECRET})
        check = project['image_updates']
        check.update(summary=SECRET, checked_at=SECRET, compose_context=['EMPTY_CONTEXT', SECRET],
                     stdout=SECRET, stderr=SECRET, exception=SECRET)
        row = check['images'][0]
        row.update(service=SECRET, image=SECRET, container_name=SECRET, container_id=SECRET,
                   image_id=SECRET, platform=SECRET, os=SECRET, note=SECRET, command=SECRET,
                   repo_digests=[SECRET], local_descriptor={'digest': SECRET, 'mediaType': SECRET},
                   remote_descriptor={'secret': SECRET}, environment={'PASSWORD': SECRET}, credentials=SECRET)
        text = report(host, project)
        self.assertNotIn(SECRET, text)
        self.assertNotIn('PASSWORD', text)
        self.assertNotIn('\x1b', text)
        self.assertIn('Service 1 (anonymisiert)', text)
        self.assertIn('Image 1 (anonymisiert)', text)

    def test_control_characters_and_invalid_versions_are_not_exported(self):
        host, project = fixture()
        host.update(docker_version='Docker version 29.8.1, build ' + SECRET,
                    compose_version='Docker Compose version v5.5.1\x1b[31m')
        row = project['image_updates']['images'][0]
        row.update(reason='context\x1b[31m', note='\x1b[31m' + SECRET, status=SECRET)
        text = report(host, project)
        self.assertNotIn(SECRET, text)
        self.assertNotIn('\x1b', text)
        self.assertIn('Docker-Version: nicht erfasst', text)
        self.assertIn('Compose-Version: nicht erfasst', text)

    def test_no_addresses_paths_or_names(self):
        host, project = fixture()
        text = report(host, project)
        for value in ('private.example', '192.168.9.123', '/srv/private', 'private-project', 'private-service'):
            self.assertNotIn(value, text)

    def test_current_update_and_mixed_results(self):
        host, project = fixture()
        row = project['image_updates']['images'][0]
        for state in ('current', 'update_available'):
            row.update(status=state, reason='', local_descriptor={'mediaType': images.INDEX, 'digest': 'sha256:' + 'a' * 64})
            text = report(host, project)
            self.assertIn('Image-Identität prüfbar: ja', text)
            self.assertIn('Lokaler Descriptor vorhanden: ja', text)
            self.assertIn('Plattformvergleich: erfolgreich', text)
        project['image_updates']['images'].append({'status': 'uncheckable', 'reason': 'identity'})
        text = report(host, project)
        self.assertIn('Update erkannt: ja', text)
        self.assertIn('Freigabe möglich: nein', text)
        self.assertIn('Prüfstatus: Prüfung unvollständig', text)

    def test_proven_candidate_matches_existing_policy(self):
        host, project = fixture()
        row = project['image_updates']['images'][0]
        row.update(status='update_available', reason='', container_id='c' * 64,
                   platform='linux/amd64', comparison_level='index',
                   local_descriptor={'mediaType': images.INDEX, 'digest': 'sha256:' + 'a' * 64},
                   remote_descriptor={'mediaType': images.INDEX, 'digest': 'sha256:' + 'b' * 64})
        self.assertIn('Freigabe möglich: ja', report(host, project))

    def test_context_success_and_empty_not_proof_of_absent_dotenv(self):
        host, project = fixture()
        check = project['image_updates']
        check.update(compose_identity=['a' * 64, 'b' * 64, []], compose_context=['EMPTY_CONTEXT', ''])
        text = report(host, project)
        self.assertIn('Standard-.env vorhanden: nicht erfasst', text)
        self.assertIn('Compose-Config-Auflösung: erfolgreich', text)
        check['compose_context'] = ['PROJECT_DOTENV_CONTEXT', '/srv/private/.env']
        self.assertIn('Standard-.env vorhanden: ja', report(host, project))

    def test_known_identity_error_does_not_invent_missing_descriptor(self):
        host, project = fixture('identity')
        text = report(host, project)
        self.assertIn('Image-Identität prüfbar: nein', text)
        self.assertIn('Lokaler Descriptor vorhanden: nicht erfasst', text)
        self.assertNotIn('IMAGE_DESCRIPTOR_MISSING', text)


class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def dialog(self):
        host, project = fixture()
        dialog = DockerDiagnosticsDialog(None, host, project)
        self.addCleanup(dialog.close)
        return dialog

    def test_privacy_notice_german_and_english(self):
        original = QtCore.QLocale()
        self.addCleanup(QtCore.QLocale.setDefault, original)
        for locale, expected in (('de_DE', PRIVACY_DE), ('en_US', PRIVACY_EN)):
            QtCore.QLocale.setDefault(QtCore.QLocale(locale))
            dialog = self.dialog()
            self.assertEqual(dialog.privacy_label.text(), expected)
            self.assertTrue(dialog.privacy_label.wordWrap())
            self.assertEqual(dialog.privacy_label.textFormat(), QtCore.Qt.TextFormat.PlainText)
            dialog.show()
            self.app.processEvents()
            self.assertTrue(dialog.privacy_label.isVisible())
            self.assertEqual([b.text() for b in dialog.findChildren(QtWidgets.QPushButton)],
                             ['Bericht speichern...', 'Schließen'])
            dialog.close()

    def test_cancel_does_not_write(self):
        dialog = self.dialog()
        with mock.patch.object(QtWidgets.QFileDialog, 'getSaveFileName', return_value=('', '')), \
             mock.patch.object(Path, 'write_text') as write:
            dialog.save_button.click()
        write.assert_not_called()

    def test_utf8_export_and_snapshot(self):
        host, project = fixture()
        dialog = DockerDiagnosticsDialog(None, host, project)
        self.addCleanup(dialog.close)
        project['image_updates']['images'][0]['reason'] = 'auth'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'diagnose.txt'
            with mock.patch.object(QtWidgets.QFileDialog, 'getSaveFileName', return_value=(str(path), '')) as picker:
                dialog.save_button.click()
            exported = path.read_bytes()
            self.assertNotIn(b'\r', exported)
            self.assertIn('Prüfergebnis'.encode('utf-8'), exported)
            text = exported.decode('utf-8')
            self.assertEqual(text, dialog.text.toPlainText())
            self.assertIn('Fehlerklasse: context', text)
            self.assertRegex(picker.call_args.args[2], r'^ssh-updater-docker-diagnose-\d{8}-\d{6}\.txt$')

    def test_write_error_is_safe_and_understandable(self):
        dialog = self.dialog()
        with mock.patch.object(QtWidgets.QFileDialog, 'getSaveFileName', return_value=('/not/exported', '')), \
             mock.patch.object(Path, 'write_text', side_effect=OSError(SECRET)), \
             mock.patch.object(QtWidgets.QMessageBox, 'warning') as warning:
            dialog.save_report()
        self.assertIn('Schreibrechte', warning.call_args.args[2])
        self.assertNotIn(SECRET, str(warning.call_args))

    def test_preview_selection_themes_and_no_remote_or_mutation(self):
        host, project = fixture()
        host['projects'] = [project, {'name': 'unchecked'}]
        themes = Path(__file__).resolve().parents[1] / 'src/sshupdater/assets/qss'
        with ExitStack() as stack:
            guards = [stack.enter_context(mock.patch.object(obj, name, side_effect=AssertionError(name)))
                      for obj, name in ((ssh_connection, 'connect_host'), (images, 'capture'),
                                        (images.Checker, 'remote'), (docker_pull.PullRun, 'run'),
                                        (docker_apply.ApplyRun, 'run'), (docker_preflight, 'preflight'))]
            for theme in ('light', 'dark', 'colour'):
                preview = DockerUpdatePreviewDialog(None, [host])
                self.addCleanup(preview.close)
                preview.setStyleSheet((themes / (theme + '.qss')).read_text(encoding='utf-8'))
                preview.show()
                preview.analysis_button.click()
                dialog, = preview.findChildren(DockerDiagnosticsDialog)
                self.app.processEvents()
                self.assertTrue(dialog.isVisible())
                self.assertTrue(dialog.text.isReadOnly())
                self.assertIn('private-project', dialog.project_label.text())
                # Qt's layout minimum depends on platform font metrics and style.
                original_size = dialog.size()
                dialog.resize(original_size + QtCore.QSize(100, 80))
                self.app.processEvents()
                self.assertGreater(dialog.width(), original_size.width())
                self.assertGreater(dialog.height(), original_size.height())
                dialog.resize(original_size)
                self.app.processEvents()
                self.assertEqual(dialog.size(), original_size)
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / 'report.txt'
                    with mock.patch.object(QtWidgets.QFileDialog, 'getSaveFileName', return_value=(str(path), '')):
                        dialog.save_button.click()
                    self.assertIn('Fehlerklasse: context', path.read_text(encoding='utf-8'))
                root = preview.projects.topLevelItem(0)
                preview.projects.setCurrentItem(root)
                self.assertFalse(preview.analysis_button.isEnabled())
                preview.projects.setCurrentItem(root.child(1))
                self.assertFalse(preview.analysis_button.isEnabled())
                preview.projects.setCurrentItem(root.child(0))
                self.assertTrue(preview.analysis_button.isEnabled())
                preview.close()
            for guard in guards:
                guard.assert_not_called()
