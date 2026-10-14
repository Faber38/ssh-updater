"""Presentation corrections may use only validated diagnostic evidence."""
import json
import unittest

from sshupdater.core.docker_diagnostic_codes import DIAGNOSTICS
from sshupdater.core.docker_image_updates import REASONS
from sshupdater.docker_diagnostics import report
from sshupdater import ui_resources
from test_docker_diagnostics import fixture, NOW, SECRET


class AuditRegressionTests(unittest.TestCase):
    def render(self, reason, code=None, **fields):
        host, project = fixture(reason)
        project['image_updates']['images'][0].update(diagnostic_code=code, **fields)
        return report(host, project, exported_at=NOW)

    def test_specific_description_wins_without_reclassifying_auth_de_en(self):
        host, project = fixture('auth')
        project['image_updates']['images'][0]['diagnostic_code'] = 'COMPOSE_CONFIG_FAILED'
        for language, index in (('de', 1), ('en', 2)):
            text = report(host, project, language=language)
            self.assertIn('Fehlerklasse: auth', text)
            self.assertIn('Beschreibung: ' + DIAGNOSTICS['COMPOSE_CONFIG_FAILED'][index], text)
            self.assertNotIn(REASONS['auth'], text)
            self.assertIn('Registry-Fehlerklasse: nicht erfasst', text)
            self.assertIn('Prüfstatus: Prüfung unvollständig', text)
            self.assertIn('Freigabe möglich: nein', text)

    def test_proven_missing_dotenv_does_not_prove_config_or_descriptor_failure(self):
        text = self.render('context', 'COMPOSE_ENV_EXPECTED_BUT_ABSENT')
        for line in ('Standard-.env vorhanden: nein', 'Kontextprüfung: fehlgeschlagen',
                     'Prüfphase: Compose-Kontextprüfung', 'Compose-Config-Auflösung: nicht erfasst',
                     'Lokaler Descriptor vorhanden: nicht erfasst'):
            self.assertIn(line, text)

    def test_config_failure_evidence_is_limited_to_config_resolution(self):
        for reason, code in (('auth', 'COMPOSE_CONFIG_FAILED'),
                             ('invalid_output', 'COMPOSE_CONFIG_INVALID_JSON'),
                             ('context', 'COMPOSE_CONFIG_STDERR')):
            with self.subTest(code=code):
                text = self.render(reason, code)
                for line in ('Compose-Config-Auflösung: fehlgeschlagen',
                             'Prüfphase: Compose-Config-Auflösung',
                             'Standard-.env vorhanden: nicht erfasst',
                             'Lokaler Descriptor vorhanden: nicht erfasst'):
                    self.assertIn(line, text)
                self.assertNotIn('Kontextprüfung: erfolgreich', text)

    def test_context_drift_does_not_prove_absent_dotenv_or_config_failure(self):
        text = self.render('config', 'COMPOSE_CONTEXT_CHANGED')
        self.assertIn('Kontextprüfung: fehlgeschlagen', text)
        self.assertIn('Prüfphase: Compose-Kontextprüfung', text)
        self.assertIn('Standard-.env vorhanden: nicht erfasst', text)
        self.assertIn('Compose-Config-Auflösung: nicht erfasst', text)

    def test_missing_descriptor_does_not_invent_other_evidence(self):
        text = self.render('identity', 'IMAGE_DESCRIPTOR_MISSING')
        self.assertIn('Lokaler Descriptor vorhanden: nein', text)
        self.assertIn('Prüfphase: Lokale Image-Identitätsprüfung', text)
        for field in ('Standard-.env vorhanden', 'Kontextprüfung', 'Compose-Config-Auflösung'):
            self.assertIn(field + ': nicht erfasst', text)

    def test_absent_codes_leave_optional_information_unknown(self):
        for reason in ('identity', 'auth', 'config'):
            text = self.render(reason)
            for field in ('Standard-.env vorhanden', 'Kontextprüfung',
                          'Compose-Config-Auflösung', 'Lokaler Descriptor vorhanden'):
                self.assertIn(field + ': nicht erfasst', text)
            self.assertIn('Genauere Ursache im Prüfergebnis nicht erfasst.', text)

    def test_invalid_codes_are_identical_to_legacy_results(self):
        for reason in ('auth', 'context', 'identity', 'config'):
            for code in ('UNKNOWN', 'COMPOSE_CONFIG_FAILED\n' + SECRET, SECRET, [], {}, 42,
                         'COMPOSE_CONFIG_FAILED' if reason != 'auth' else 'IMAGE_DESCRIPTOR_MISSING'):
                with self.subTest(reason=reason, code=code):
                    self.assertEqual(self.render(reason, code), self.render(reason))

    def test_success_state_does_not_accept_failure_code_evidence(self):
        text = self.render('identity', 'IMAGE_DESCRIPTOR_MISSING', status='current')
        self.assertIn('Lokaler Descriptor vorhanden: nicht erfasst', text)
        self.assertNotIn('IMAGE_DESCRIPTOR_MISSING', text)

    def test_allowlisted_descriptions_and_sentinels_de_en(self):
        for code, (reason, german, english) in DIAGNOSTICS.items():
            host, project = fixture(reason or ('auth' if code == 'COMPOSE_CONFIG_FAILED' else 'identity'))
            project['image_updates']['images'][0].update(diagnostic_code=code, note=SECRET,
                exception=SECRET, stderr=SECRET, environment={'PASSWORD': SECRET},
                service=SECRET, image=SECRET, local_descriptor={'digest': SECRET})
            for language, description in (('de', german), ('en', english)):
                with self.subTest(code=code, language=language):
                    text = report(host, project, language=language)
                    self.assertIn('Beschreibung: ' + description, text)
                    self.assertNotIn(SECRET, text)
                    self.assertNotIn('PASSWORD', text)

    def test_help_uses_user_language_and_github_privacy_notice_de_en(self):
        topics = json.loads(ui_resources.resource_path('assets', 'help', 'topics.json').read_text(encoding='utf-8'))
        body = next(c['body'] for t in topics for c in t.get('children', []) if c['id'] == 'docker-status')
        self.assertNotIn('Qt-Sprache', body)
        self.assertNotIn('Vorschau-Snapshot', body)
        self.assertIn('Sprache der Oberfläche', body)
        self.assertIn('gespeicherten Stand der Vorschau', body)
        for sentence in (
            'Der Diagnosebericht bleibt auf deinem Rechner, bis du ihn selbst weitergibst.',
            'Eine automatische Übertragung findet nicht statt.',
            'Wenn du einen Diagnosebericht in einem öffentlichen GitHub-Issue veröffentlichst, können auch andere Personen ihn lesen und herunterladen.',
            'Bitte prüfe den Bericht vor der Weitergabe.',
            'The diagnostic report remains on your computer until you choose to share it.',
            'No automatic transmission takes place.',
            'If you publish a diagnostic report in a public GitHub issue, other people can read and download it.',
            'Please review the report before sharing it.',
        ):
            self.assertEqual(body.count(sentence), 1)
