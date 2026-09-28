from dataclasses import FrozenInstanceError
from copy import deepcopy
from pathlib import Path
from unittest import mock
import unittest
import test_docker_preview as fixtures
from PyQt6 import QtCore, QtWidgets
from sshupdater import ui_main
from sshupdater.core import db, ssh_client, docker_image_updates
from sshupdater.ui_docker_preview import DockerUpdatePreviewDialog


class PlanTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.PreviewTests.setUpClass.__func__)
    setUp = fixtures.PreviewTests.setUp
    window = fixtures.PreviewTests.window
    result = fixtures.PreviewTests.result
    discovery = fixtures.PreviewTests.discovery
    show_projects = fixtures.PreviewTests.show_projects

    def project(self, name='update', *states):
        project = fixtures.PreviewTests.project(self, name, *(states or ('update_available',)))
        for image in project['image_updates']['images']:
            image.update(comparison_level='platform_manifest',
                         local_descriptor={'mediaType': 'application/vnd.oci.image.manifest.v1+json', 'digest': 'sha256:'+'a'*64},
                         remote_descriptor={'mediaType': 'application/vnd.oci.image.manifest.v1+json', 'digest': 'sha256:'+'b'*64})
        return project

    def approve(self, w, projects=None, host=1):
        parent = self.show_projects(w, projects or [self.project()], host)
        for row in range(parent.rowCount()):
            parent.child(row, 0).setCheckState(QtCore.Qt.CheckState.Checked)
        w.act_docker_preview.trigger()
        return parent

    def test_approval_filters_candidates_and_is_immutable_secret_free(self):
        w = self.window()
        projects = [self.project('a-update'), self.project('b-current', 'current'),
                    self.project('c-error', 'uncheckable'), self.project('d-pinned', 'digest_pinned'),
                    self.project('e-build', 'local_build'), self.project('f-partial', 'update_available', 'uncheckable')]
        projects[0]['password'] = 'SECRET'
        projects[0]['image_updates']['images'][0].update(token='SECRET', note='SECRET', command='SECRET')
        self.approve(w, projects)
        plan = w._docker_plan
        self.assertTrue(w.act_docker_update.isEnabled())
        self.assertEqual([p.name for p in plan.candidates], ['a-update'])
        self.assertEqual(len(plan.selection), 6)
        self.assertNotIn('SECRET', repr(plan))
        with self.assertRaises(FrozenInstanceError):
            plan.candidates = ()
        projects[0]['image_updates']['images'][0]['platform'] = 'changed'
        self.assertEqual(plan.candidates[0].images[0][4], 'linux/amd64')

    def test_preview_and_plan_share_project_eligibility(self):
        for other in (None, 'current', 'uncheckable', 'digest_pinned', 'local_build'):
            with self.subTest(other=other):
                w = self.window()
                states = ('update_available',) + ((other,) if other else ())
                self.approve(w, [self.project('mixed', *states)])
                dialog, = w.findChildren(DockerUpdatePreviewDialog)
                expected = int(other in (None, 'current'))
                self.assertEqual(dialog.counts['candidate'], expected)
                self.assertEqual(dialog.counts['updates'], 1)
                self.assertEqual(len(w._docker_plan.candidates) if w._docker_plan else 0, expected)
                self.assertEqual(w.act_docker_update.isEnabled(), bool(expected))
                text = dialog.details.toPlainText()
                self.assertIn('↑ Image-Update', text)
                if expected:
                    self.assertIn('Neues Image', text)
                else:
                    self.assertIn('Projekt nicht zur Aktualisierung freigegeben', text)
                    self.assertNotIn('Geplanter späterer Ablauf', text)

    def test_preview_counts_multiple_updates_separately_from_eligible_projects(self):
        w = self.window()
        self.approve(w, [self.project('ready', 'update_available', 'update_available'),
                         self.project('blocked', 'update_available', 'uncheckable'),
                         self.project('current', 'current')])
        dialog, = w.findChildren(DockerUpdatePreviewDialog)
        self.assertEqual(dialog.counts['updates'], 3)
        self.assertEqual(dialog.counts['candidate'], 1)
        self.assertEqual([p.name for p in w._docker_plan.candidates], ['ready'])
        self.assertTrue(w.act_docker_update.isEnabled())

    def test_missing_or_conflicting_evidence_never_approves(self):
        for field, value in [('remote_descriptor', None), ('comparison_level', 'unknown'),
                             ('platform', 'unknown/unknown')]:
            w = self.window()
            project = self.project()
            project['image_updates']['images'][0][field] = value
            self.approve(w, [project])
            self.assertIsNone(w._docker_plan)
            self.assertFalse(w.act_docker_update.isEnabled())

    def test_selection_change_invalidates_without_resurrection(self):
        w = self.window()
        parent = self.approve(w, [self.project('a'), self.project('b')])
        parent.child(1, 0).setCheckState(QtCore.Qt.CheckState.Unchecked)
        self.assertFalse(w.act_docker_update.isEnabled())
        parent.child(1, 0).setCheckState(QtCore.Qt.CheckState.Checked)
        self.assertIsNone(w._docker_plan)
        w.act_docker_preview.trigger()
        self.assertTrue(w.act_docker_update.isEnabled())
        self.show_projects(w, [self.project('a'), self.project('b'), self.project('c')])
        w.act_docker_preview.trigger()
        parent.child(2, 0).setCheckState(QtCore.Qt.CheckState.Checked)
        self.assertIsNone(w._docker_plan)

    def test_new_check_and_identical_result_invalidate(self):
        w = self.window()
        parent = self.approve(w)
        self.show_projects(w, [self.project()])
        self.assertIsNone(w._docker_plan)
        w.act_docker_preview.trigger()
        self.assertTrue(w.act_docker_update.isEnabled())
        parent.setCheckState(QtCore.Qt.CheckState.Checked)
        with mock.patch.object(w, '_prepare_passwords', return_value=True), mock.patch.object(ui_main._CheckWorker, 'start'):
            w._on_check()
        self.assertIsNone(w._docker_plan)
        self.assertFalse(w.act_docker_update.isEnabled())

    def test_all_plan_identity_fields_invalidate(self):
        mutations = [lambda p: p.update(name='renamed'), lambda p: p.update(config_files=['/new/path']),
                     lambda p: p.update(status='exited'), lambda p: p['image_updates'].update(checked_at='new')]
        for field, value in [('service','other'), ('container_name','new-container'), ('image','other:tag'),
                             ('platform','linux/arm64'), ('status','current'), ('image_id','changed'),
                             ('remote_descriptor', {'mediaType':'other','digest':'sha256:'+'c'*64})]:
            mutations.append(lambda p, f=field, v=value: p['image_updates']['images'][0].update({f:v}))
        for mutation in mutations:
            w = self.window()
            self.approve(w)
            mutation(w._docker_results[1]['projects'][0])
            w._sync_docker_actions()
            self.assertIsNone(w._docker_plan)
            self.assertFalse(w.act_docker_update.isEnabled())

    def test_connection_changes_and_deletion_invalidate(self):
        original = deepcopy(self.hosts)
        for field, value in [('primary_ip','changed'), ('port',2222), ('user','other'),
                             ('auth_method','password'), ('key_path','/new/key'), ('password_enc',b'SECRET')]:
            self.hosts = deepcopy(original)
            w = self.window()
            self.approve(w)
            self.hosts[0][field] = value
            with mock.patch.object(db, 'list_hosts', return_value=self.hosts):
                w._reload_hosts()
            self.assertIsNone(w._docker_plan)
        self.hosts = deepcopy(original)
        w = self.window()
        self.approve(w)
        with mock.patch.object(db, 'list_hosts', return_value=self.hosts[1:]):
            w._reload_hosts()
        self.assertIsNone(w._docker_plan)

    def test_multihost_result_discard_and_project_removal_invalidate_whole_plan(self):
        for change in ('result', 'discard', 'remove'):
            w = self.window()
            self.approve(w, host=1)
            self.approve(w, host=2)
            self.assertEqual(len(w._docker_plan.candidates), 2)
            if change == 'result':
                self.show_projects(w, [self.project()], 1)
            elif change == 'discard':
                self.result(w, None, host_id=1)
            else:
                self.show_projects(w, [], 1)
            self.assertIsNone(w._docker_plan)
            self.assertFalse(w.act_docker_update.isEnabled())

    def test_display_actions_close_reload_and_theme_preserve_plan(self):
        w = self.window()
        parent = self.approve(w)
        plan = w._docker_plan
        dialog, = w.findChildren(DockerUpdatePreviewDialog)
        dialog.close()
        w.table.collapse(parent.index())
        w.table.expand(parent.index())
        w.table.setCurrentIndex(parent.index())
        w.table.setColumnWidth(1, 230)
        w.move(20, 20)
        with mock.patch.object(ui_main, 'DockerDetailsDialog'):
            w._open_docker_details(w.table.model().index(0, 6))
        with mock.patch.object(db, 'list_hosts', return_value=self.hosts):
            w._reload_hosts()
        themes = Path(ui_main.__file__).parent / 'assets' / 'qss'
        for theme in ('light','dark','colour'):
            w.setStyleSheet((themes / (theme+'.qss')).read_text(encoding='utf-8'))
            w._sync_docker_actions()
            self.assertIs(w._docker_plan, plan)
            self.assertTrue(w.act_docker_update.isEnabled())

    def test_new_preview_replaces_plan_and_restart_is_empty(self):
        w = self.window()
        self.approve(w)
        old = w._docker_plan
        w.act_docker_preview.trigger()
        self.assertIsNot(w._docker_plan, old)
        self.show_projects(w, [self.project('update', 'current')])
        w.act_docker_preview.trigger()
        self.assertIsNone(w._docker_plan)
        self.assertFalse(w.act_docker_update.isEnabled())
        other = self.window()
        self.assertIsNone(other._docker_plan)
        self.assertFalse(other.act_docker_update.isEnabled())

    def test_active_update_button_starts_only_preflight_without_persistence(self):
        w = self.window()
        before = [a.isEnabled() for a in (w.act_check,w.act_sim,w.act_upg,w.act_clean,w.act_reboot)]
        with mock.patch.object(ssh_client, 'connect_host', side_effect=AssertionError('SSH')), \
             mock.patch.object(docker_image_updates, 'capture', side_effect=AssertionError('remote')), \
             mock.patch.object(docker_image_updates.Checker, 'remote', side_effect=AssertionError('registry')):
            self.approve(w)
            self.assertTrue(w.act_docker_update.isEnabled())
            with mock.patch.object(db, 'set_check_result', side_effect=AssertionError('DB')), \
                 mock.patch.object(db, 'list_hosts', return_value=self.hosts), \
                 mock.patch.object(ui_main._DockerPreflightWorker, 'start') as start:
                w.act_docker_update.trigger()
                start.assert_called_once()
                worker = w.preflight_worker
                worker.result = {'status': 'passed', 'note': 'Read-only test', 'projects': []}
                w._on_preflight_done()
        self.assertEqual(before, [a.isEnabled() for a in (w.act_check,w.act_sim,w.act_upg,w.act_clean,w.act_reboot)])
