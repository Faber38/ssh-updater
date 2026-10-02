"""Product trust workflow: isolated vaults, GUI decisions and loopback SSH only."""
import asyncio
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import asyncssh
from PyQt6 import QtCore, QtWidgets
from sshupdater import app as application, ui_host_keys as ui, ui_config
from sshupdater.core import crypto, credentials, db, host_keys, settings, ssh_connection, storage


class Fixture:
    @classmethod
    def setUpClass(cls):
        cls.qt = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for module, name, value in (
            (settings, 'DATA_DIR', self.root), (settings, 'KNOWN_HOSTS', self.root/'known_hosts'),
            (settings, 'KNOWN_HOSTS_MAC', self.root/'known_hosts.mac'),
            (crypto, 'DATA_DIR', self.root), (crypto, '_SALT_PATH', self.root/'vault.salt'),
            (crypto, '_VERIFIER_PATH', self.root/'vault.verify'),
            (crypto, '_FERNET', None), (crypto, '_MAC_KEY', None), (db, 'DB_PATH', self.root/'app.db')):
            patch = mock.patch.object(module, name, value)
            patch.start(); self.addCleanup(patch.stop)
        crypto.set_master_password('old')  # Existing policy deliberately unchanged.
        db.init_db()
        host_keys.initialize_fresh()
        self.key = asyncssh.generate_private_key('ssh-ed25519').convert_to_public()
        self.hid = db.add_or_update_host(proxmox_uid=None, name='first', primary_ip='one.invalid',
                                        user='root', port=22, auth_method='password', password_plain='SYNTHETIC-SECRET')
        self.host = db.get_host(self.hid)
        self.target = ui.Target((self.hid,), ('first',), 'one.invalid', 22)

    def observation(self, target=None, previous=None):
        target = target or self.target
        return host_keys.Observation(target.address, target.port, self.key, previous)

    def dialog(self, results):
        with mock.patch.object(ui.QtCore.QTimer, 'singleShot'):
            dialog = ui.HostKeyDialog([r.target for r in results])
        dialog.worker = SimpleNamespace(cancelled=False, integrity_error=None, results=results,
                                        isRunning=lambda: False)
        dialog._received()
        self.addCleanup(dialog.deleteLater)
        return dialog


class TrustWorkflowTests(Fixture, unittest.TestCase):
    def test_no_selection_only_missing_pins_and_shared_endpoints(self):
        other = db.add_or_update_host(proxmox_uid=None, name='other', primary_ip='two.invalid')
        duplicate = db.add_or_update_host(proxmox_uid=None, name='alias row', primary_ip='ONE.invalid')
        targets = ui.inspection_targets(db.list_hosts(), [])
        self.assertEqual(len(targets), 2)
        shared = next(t for t in targets if t.endpoint == self.target.endpoint)
        self.assertEqual(set(shared.host_ids), {self.hid, duplicate})
        host_keys.confirm(self.observation())
        self.assertEqual([t.host_ids for t in ui.inspection_targets(db.list_hosts(), [])], [(other,)])
        selected = ui.inspection_targets(db.list_hosts(), [self.hid, duplicate])
        self.assertEqual(len(selected), 1)
        self.assertEqual(set(selected[0].host_ids), {self.hid, duplicate})

    def test_selected_valid_hosts_ignore_unselected_invalid_host(self):
        second = db.add_or_update_host(proxmox_uid=None, name='second', primary_ip='two.invalid')
        db.add_or_update_host(proxmox_uid=None, name='invalid', primary_ip='bad host')
        for selected in ([self.hid], [self.hid, second]):
            with self.subTest(selected=selected):
                targets = ui.inspection_targets(db.list_hosts(), selected)
                self.assertEqual({t.host_ids[0] for t in targets}, set(selected))
                worker = ui._ProbeWorker(targets)
                with mock.patch.object(ssh_connection, 'inspect_host', new=mock.AsyncMock(
                        side_effect=[self.observation(t) for t in targets])) as probe:
                    worker.run()
                self.assertEqual(probe.call_count, len(selected))
                self.assertTrue(all(r.observation is not None and not r.error for r in worker.results))

    def test_invalid_candidates_are_results_without_blocking_valid_hosts(self):
        second = db.add_or_update_host(proxmox_uid=None, name='second', primary_ip='two.invalid')
        bad = db.add_or_update_host(proxmox_uid=None, name='invalid', primary_ip='bad host')
        for selected in ([], [self.hid, bad, second]):
            with self.subTest(selected=selected):
                targets = ui.inspection_targets(db.list_hosts(), selected)
                worker = ui._ProbeWorker(targets)
                valid = [t for t in targets if not t.error]
                with mock.patch.object(ssh_connection, 'inspect_host', new=mock.AsyncMock(
                        side_effect=[self.observation(t) for t in valid])) as probe:
                    worker.run()
                self.assertEqual(probe.call_count, 2)
                self.assertEqual({c.args[0]['primary_ip'] for c in probe.call_args_list},
                                 {'one.invalid', 'two.invalid'})
                self.assertEqual(len(worker.results), 3)
                failed = [r for r in worker.results if r.error]
                self.assertEqual(len(failed), 1)
                self.assertEqual(failed[0].target.host_ids, (bad,))
                self.assertIsNone(failed[0].observation)
                dialog = self.dialog(worker.results)
                row = worker.results.index(failed[0])
                self.assertIn('Fehler:', dialog.table.item(row, 3).text())
                self.assertIn('bad host', dialog.table.item(row, 2).text())
                self.assertFalse(dialog.table.item(row, 0).flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                self.assertEqual(host_keys.load(), {})

    def test_explicit_selection_loads_only_selected_rows(self):
        db.add_or_update_host(proxmox_uid=None, name='invalid', primary_ip='bad host')
        with mock.patch.object(ui_config.ConfigDialog, '_apply_theme_choice'):
            dialog = ui_config.ConfigDialog()
        self.addCleanup(dialog.deleteLater)
        dialog.table.selectRow(0)
        with mock.patch.object(db, 'list_hosts', side_effect=AssertionError('unselected rows loaded')), \
                mock.patch.object(db, 'get_host', wraps=db.get_host) as get_host, \
                mock.patch.object(ui, 'HostKeyDialog') as result_dialog:
            dialog._check_identity()
        get_host.assert_called_once_with(self.hid)
        self.assertEqual([t.host_ids for t in result_dialog.call_args.args[0]], [(self.hid,)])

    def test_selected_shared_endpoint_excludes_unselected_row_but_shares_pin(self):
        duplicate = db.add_or_update_host(proxmox_uid=None, name='duplicate', primary_ip='ONE.invalid')
        targets = ui.inspection_targets(db.list_hosts(), [self.hid])
        self.assertEqual([t.host_ids for t in targets], [(self.hid,)])
        dialog = self.dialog([ui.ProbeResult(targets[0], self.observation())])
        dialog.table.item(0, 0).setCheckState(QtCore.Qt.CheckState.Checked)
        dialog._trust()
        self.assertEqual(ui.inspection_targets(db.list_hosts(), []), [])
        other = ui.inspection_targets(db.list_hosts(), [duplicate])
        self.assertEqual(other[0].endpoint, targets[0].endpoint)
        self.assertEqual(len(host_keys.load()), 1)

    def test_config_actual_selection_not_current_index_or_main_checks(self):
        second = db.add_or_update_host(proxmox_uid=None, name='second', primary_ip='two.invalid')
        with mock.patch.object(ui_config.ConfigDialog, '_apply_theme_choice'):
            dialog = ui_config.ConfigDialog()
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.table.selectionModel().selectedRows(), [])
        dialog.table.setCurrentCell(0, 0)
        dialog.table.clearSelection()
        self.assertGreaterEqual(dialog.table.currentRow(), 0)
        with mock.patch.object(ui, 'HostKeyDialog') as result_dialog:
            dialog._check_identity()
            self.assertEqual(len(result_dialog.call_args.args[0]), 2)
            dialog.table.selectRow(1)
            dialog._check_identity()
            self.assertEqual([t.host_ids for t in result_dialog.call_args.args[0]], [(second,)])

    def test_result_nothing_preselected_only_checked_endpoint_saved(self):
        second = db.add_or_update_host(proxmox_uid=None, name='second', primary_ip='two.invalid')
        target2 = ui.Target((second,), ('second',), 'two.invalid', 22)
        dialog = self.dialog([ui.ProbeResult(self.target, self.observation()),
                              ui.ProbeResult(target2, self.observation(target2))])
        self.assertFalse(dialog.trust.isEnabled())
        dialog._trust()
        self.assertEqual(host_keys.load(), {})
        dialog.table.item(0, 0).setCheckState(QtCore.Qt.CheckState.Checked)
        dialog._trust()
        self.assertEqual(set(host_keys.load()), {self.target.endpoint})

    def test_known_and_failed_rows_not_selectable(self):
        dialog = self.dialog([ui.ProbeResult(self.target, self.observation(previous=self.key)),
                              ui.ProbeResult(self.target, error='timeout')])
        for row in range(2):
            self.assertFalse(dialog.table.item(row, 0).flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable)

    def test_changed_key_requires_individual_warning_cancel_preserves_old(self):
        old = asyncssh.generate_private_key('ssh-ed25519').convert_to_public()
        host_keys.confirm(host_keys.Observation('one.invalid', 22, old))
        dialog = self.dialog([ui.ProbeResult(self.target, self.observation(previous=old))])
        dialog.table.item(0, 0).setCheckState(QtCore.Qt.CheckState.Checked)
        with mock.patch.object(ui.PlainMessageBox, 'warning', return_value=ui.PlainMessageBox.StandardButton.Cancel) as warn:
            dialog._trust()
        self.assertEqual(host_keys.load()[self.target.endpoint], old)
        text = warn.call_args.args[2]
        self.assertIn(old.get_fingerprint('sha256'), text)
        self.assertIn(self.key.get_fingerprint('sha256'), text)
        self.assertEqual(warn.call_args.args[-1], ui.PlainMessageBox.StandardButton.Cancel)
        dialog.table.item(0, 0).setCheckState(QtCore.Qt.CheckState.Checked)
        with mock.patch.object(ui.PlainMessageBox, 'warning', return_value=ui.PlainMessageBox.StandardButton.Yes):
            dialog._trust()
        self.assertEqual(host_keys.load()[self.target.endpoint], self.key)

    def test_stale_target_or_deleted_host_cannot_confirm(self):
        for current in (None, dict(self.host, primary_ip='elsewhere.invalid'), dict(self.host, port=2222)):
            dialog = self.dialog([ui.ProbeResult(self.target, self.observation())])
            dialog.table.item(0, 0).setCheckState(QtCore.Qt.CheckState.Checked)
            with mock.patch.object(db, 'get_host', return_value=current), mock.patch.object(host_keys, 'confirm') as confirm:
                dialog._trust()
            confirm.assert_not_called()
            self.assertIn('veraltet', dialog.table.item(0, 3).text())

    def test_database_error_before_confirmation_is_reported_without_write(self):
        dialog = self.dialog([ui.ProbeResult(self.target, self.observation())])
        dialog.table.item(0, 0).setCheckState(QtCore.Qt.CheckState.Checked)
        with mock.patch.object(db, 'get_host', side_effect=db.sqlite3.OperationalError('database locked')), \
                mock.patch.object(host_keys, 'confirm') as confirm:
            dialog._trust()
        confirm.assert_not_called()
        self.assertIn('database locked', dialog.table.item(0, 3).text())

    def test_stale_pin_not_overwritten(self):
        dialog = self.dialog([ui.ProbeResult(self.target, self.observation())])
        other = asyncssh.generate_private_key('ssh-ed25519').convert_to_public()
        host_keys.confirm(host_keys.Observation('one.invalid', 22, other))
        dialog.table.item(0, 0).setCheckState(QtCore.Qt.CheckState.Checked)
        dialog._trust()
        self.assertEqual(host_keys.load()[self.target.endpoint], other)
        self.assertIn('inzwischen', dialog.table.item(0, 3).text())

    def test_worker_isolates_errors_and_only_passes_target_fields(self):
        targets = [self.target, ui.Target((2,), ('second',), 'two.invalid', 22)]
        worker = ui._ProbeWorker(targets)
        with mock.patch.object(ssh_connection, 'inspect_host', new=mock.AsyncMock(
                side_effect=[OSError('unreachable'), self.observation(targets[1])])) as inspect:
            worker.run()
        self.assertEqual(len(worker.results), 2)
        self.assertEqual(worker.results[0].error, 'unreachable')
        self.assertIsNotNone(worker.results[1].observation)
        for call in inspect.call_args_list:
            self.assertEqual(set(call.args[0]), {'primary_ip', 'port', 'user'})

    def test_worker_global_integrity_failure_stops_and_offers_recovery(self):
        worker = ui._ProbeWorker([self.target, self.target])
        with mock.patch.object(ssh_connection, 'inspect_host', new=mock.AsyncMock(
                side_effect=host_keys.TruststoreIntegrityError('bad'))) as inspect:
            worker.run()
        self.assertEqual(inspect.call_count, 1)
        with mock.patch.object(ui.QtCore.QTimer, 'singleShot'):
            dialog = ui.HostKeyDialog([self.target])
        self.addCleanup(dialog.deleteLater)
        dialog.worker = worker
        with mock.patch.object(ui, 'recover_truststore', return_value=False) as recover:
            dialog._received()
        recover.assert_called_once()
        self.assertEqual(host_keys.load(), {})

    def test_cancel_before_deferred_probe_starts_no_worker(self):
        with mock.patch.object(ui.QtCore.QTimer, 'singleShot'):
            dialog = ui.HostKeyDialog([self.target])
        self.addCleanup(dialog.deleteLater)
        dialog.reject()
        with mock.patch.object(ui, '_ProbeWorker') as worker:
            dialog._probe()
        worker.assert_not_called()
        self.assertEqual(host_keys.load(), {})

    def test_worker_cancel_discards_results_and_never_confirms(self):
        worker = ui._ProbeWorker([self.target, self.target])
        async def inspect(_):
            return self.observation()
        with mock.patch.object(worker, 'isInterruptionRequested', side_effect=[False, True, True]), \
                mock.patch.object(ssh_connection, 'inspect_host', side_effect=inspect) as probe:
            worker.run()
        self.assertTrue(worker.cancelled)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(host_keys.load(), {})

    def test_missing_bad_mac_and_modified_store_fail_closed(self):
        host_keys.confirm(self.observation())
        raw, mac = settings.KNOWN_HOSTS.read_bytes(), settings.KNOWN_HOSTS_MAC.read_bytes()
        for content, tag in [(raw+b'#changed\n', mac), (raw, b'wrong'), (raw, None)]:
            settings.KNOWN_HOSTS.write_bytes(content)
            if tag is None:
                settings.KNOWN_HOSTS_MAC.unlink()
            else:
                settings.KNOWN_HOSTS_MAC.write_bytes(tag)
            with self.assertRaises(host_keys.TruststoreIntegrityError):
                host_keys.load()
            self.assertEqual(settings.KNOWN_HOSTS.read_bytes(), content)

    def test_recovery_preserves_each_quarantine_and_validates_empty_pair(self):
        for content in (b'first invalid', b'second invalid'):
            settings.KNOWN_HOSTS.write_bytes(content)
            host_keys.reset_for_reconfirmation()
            self.assertEqual(host_keys.load(), {})
        backups = list(self.root.glob('known_hosts.untrusted.*'))
        self.assertEqual({p.read_bytes() for p in backups}, {b'first invalid', b'second invalid'})
        self.assertEqual(len(list(self.root.glob('known_hosts.mac.untrusted.*'))), 2)

    def test_mac_write_failure_is_closed_and_recoverable(self):
        with mock.patch.object(host_keys, '_write_truststore_mac', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                host_keys.confirm(self.observation())
        with self.assertRaises(host_keys.TruststoreIntegrityError):
            host_keys.load()
        with mock.patch.object(ui.PlainMessageBox, 'warning', return_value=ui.PlainMessageBox.StandardButton.Yes):
            self.assertTrue(ui.recover_truststore(None, OSError('disk full')))
        self.assertEqual(host_keys.load(), {})

    def test_failed_recovery_does_not_report_success(self):
        settings.KNOWN_HOSTS.write_bytes(b'bad')
        with mock.patch.object(ui.PlainMessageBox, 'warning', return_value=ui.PlainMessageBox.StandardButton.Yes), \
                mock.patch.object(ui.PlainMessageBox, 'critical') as critical, \
                mock.patch.object(host_keys, '_write_truststore_mac', side_effect=OSError('disk full')):
            # Empty content would match the pre-existing empty MAC, so damage it as well.
            settings.KNOWN_HOSTS_MAC.write_bytes(b'bad')
            self.assertFalse(ui.recover_truststore(None, OSError('bad')))
        critical.assert_called_once()
        with self.assertRaises(host_keys.TruststoreIntegrityError):
            host_keys.load()

    def test_vault_bytes_tokens_policy_and_failed_unlock_unchanged(self):
        before = {p.name:p.read_bytes() for p in self.root.iterdir() if p.name in ('vault.salt','vault.verify','app.db')}
        token = self.host['password_enc']
        crypto.set_master_password('old')
        host_keys.confirm(self.observation())
        crypto.set_master_password('old')
        self.assertEqual(db.get_host_password(self.hid), 'SYNTHETIC-SECRET')
        self.assertEqual(db.get_host(self.hid)['password_enc'], token)
        self.assertEqual(before, {name:(self.root/name).read_bytes() for name in before})
        self.assertFalse((self.root/'vault.kdf').exists())
        with self.assertRaises(crypto.WrongPassword):
            crypto.set_master_password('wrong')
        self.assertIsNone(crypto._FERNET)
        self.assertIsNone(crypto._MAC_KEY)
        with self.assertRaises(host_keys.TruststoreIntegrityError):
            host_keys.load()

    def test_callbacks_only_after_trust_and_once_per_method(self):
        host_keys.confirm(self.observation())
        validator = host_keys.Validator('one.invalid', 22, 'one.invalid', credential_host=self.host)
        with mock.patch.object(crypto, 'decrypt_host_password', return_value='SYNTHETIC-SECRET') as decrypt:
            self.assertIsNone(validator.password_auth_requested())
            self.assertIsNone(validator.kbdint_auth_requested())
            decrypt.assert_not_called()
            self.assertTrue(validator.validate_host_public_key('one.invalid', '127.0.0.1', 22, self.key))
            self.assertEqual(validator.password_auth_requested(), 'SYNTHETIC-SECRET')
            self.assertIsNone(validator.password_auth_requested())
            self.assertEqual(validator.kbdint_auth_requested(), '')
            self.assertEqual(validator.kbdint_challenge_received('', '', '', []), [])
            self.assertEqual(validator.kbdint_challenge_received('', '', '', [('Password:',False)]), ['SYNTHETIC-SECRET'])
            self.assertIsNone(validator.kbdint_challenge_received('', '', '', [('Password:',False)]))
            self.assertIsNone(validator.kbdint_auth_requested())
            decrypt.assert_called_once()
            validator.connection_lost(None)
            self.assertIsNone(validator._password)

    def test_keyboard_interactive_rejects_unknown_and_multiple_prompts(self):
        host_keys.confirm(self.observation())
        for prompts in [[('OTP:',False)], [('Password:',True)], [('Password:',False),('OTP:',False)]]:
            validator = host_keys.Validator('one.invalid',22,'one.invalid',credential_host=self.host)
            validator.validate_host_public_key('one.invalid','127.0.0.1',22,self.key)
            validator.kbdint_auth_requested()
            with mock.patch.object(crypto,'decrypt_host_password') as decrypt:
                self.assertIsNone(validator.kbdint_challenge_received('','','',prompts))
                self.assertIsNone(validator.kbdint_challenge_received('','','',[('Password:',False)]))
            decrypt.assert_not_called()

    def test_startup_integrity_recovery_cancel_and_accept(self):
        settings.KNOWN_HOSTS.write_bytes(b'bad')
        for answer, expected in [(ui.PlainMessageBox.StandardButton.Cancel,1),
                                 (ui.PlainMessageBox.StandardButton.Yes,0)]:
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(application.QtWidgets,'QApplication',return_value=self.qt))
                stack.enter_context(mock.patch.object(application.settings,'initialize'))
                stack.enter_context(mock.patch.object(application,'apply_theme'))
                stack.enter_context(mock.patch.object(application.QInputDialog,'getText',return_value=('old',True)))
                stack.enter_context(mock.patch.object(ui.PlainMessageBox,'warning',return_value=answer))
                window = stack.enter_context(mock.patch.object(application,'MainWindow'))
                stack.enter_context(mock.patch.object(self.qt,'exec',return_value=0))
                self.assertEqual(application.main(),expected)
                self.assertEqual(window.called, expected == 0)
        self.assertEqual(host_keys.load(), {})


    def start_application(self, passwords, answer):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(application.QtWidgets, 'QApplication', return_value=self.qt))
            stack.enter_context(mock.patch.object(application.settings, 'initialize'))
            stack.enter_context(mock.patch.object(application, 'apply_theme'))
            stack.enter_context(mock.patch.object(application.QInputDialog, 'getText', side_effect=passwords))
            warning = stack.enter_context(mock.patch.object(ui.PlainMessageBox, 'warning', return_value=answer))
            critical = stack.enter_context(mock.patch.object(ui.PlainMessageBox, 'critical'))
            window = stack.enter_context(mock.patch.object(application, 'MainWindow'))
            stack.enter_context(mock.patch.object(self.qt, 'exec', return_value=0))
            result = application.main()
        return result, warning, critical, window

    def test_startup_fresh_vault_initializes_only_empty_trust(self):
        for path in (db.DB_PATH, crypto._SALT_PATH, crypto._VERIFIER_PATH,
                     settings.KNOWN_HOSTS, settings.KNOWN_HOSTS_MAC):
            path.unlink()
        result, warning, critical, window = self.start_application(
            [('old', True), ('old', True)], ui.PlainMessageBox.StandardButton.Cancel)
        self.assertEqual(result, 0)
        warning.assert_not_called()
        critical.assert_not_called()
        window.assert_called_once()
        self.assertEqual(host_keys.load(), {})
        self.assertTrue(crypto._SALT_PATH.exists())
        self.assertFalse((self.root/'vault.kdf').exists())

    def test_startup_legacy_cancel_preserves_bytes_then_accept_quarantines(self):
        host_keys.confirm(self.observation())
        settings.KNOWN_HOSTS_MAC.unlink()
        before = settings.KNOWN_HOSTS.read_bytes()
        result, warning, critical, window = self.start_application(
            [('old', True)], ui.PlainMessageBox.StandardButton.Cancel)
        self.assertEqual(result, 1)
        warning.assert_called_once()
        window.assert_not_called()
        self.assertEqual(settings.KNOWN_HOSTS.read_bytes(), before)
        self.assertFalse(settings.KNOWN_HOSTS_MAC.exists())
        result, _, critical, window = self.start_application(
            [('old', True)], ui.PlainMessageBox.StandardButton.Yes)
        self.assertEqual(result, 0)
        critical.assert_not_called()
        window.assert_called_once()
        self.assertEqual(host_keys.load(), {})
        self.assertEqual(next(self.root.glob('known_hosts.untrusted.*')).read_bytes(), before)

    def test_dialog_mac_write_failure_offers_recovery(self):
        dialog = self.dialog([ui.ProbeResult(self.target, self.observation())])
        dialog.table.item(0, 0).setCheckState(QtCore.Qt.CheckState.Checked)
        with mock.patch.object(host_keys, '_write_truststore_mac', side_effect=OSError('disk full')), \
                mock.patch.object(ui, 'recover_truststore', return_value=False) as recover:
            dialog._trust()
        recover.assert_called_once()
        with self.assertRaises(host_keys.TruststoreIntegrityError):
            host_keys.load()

    def test_quarantine_failure_keeps_original_active_files(self):
        settings.KNOWN_HOSTS.write_bytes(b'bad')
        before = (settings.KNOWN_HOSTS.read_bytes(), settings.KNOWN_HOSTS_MAC.read_bytes())
        original = storage.write_private
        def fail_backup(path, *args, **kwargs):
            if '.untrusted.' in path.name:
                raise OSError('no space for backup')
            return original(path, *args, **kwargs)
        with mock.patch.object(storage, 'write_private', side_effect=fail_backup):
            with self.assertRaises(OSError):
                host_keys.reset_for_reconfirmation()
        self.assertEqual((settings.KNOWN_HOSTS.read_bytes(), settings.KNOWN_HOSTS_MAC.read_bytes()), before)

    def test_changed_key_rejection_does_not_prevent_other_confirmation(self):
        second = db.add_or_update_host(proxmox_uid=None, name='second', primary_ip='two.invalid')
        target2 = ui.Target((second,), ('second',), 'two.invalid', 22)
        old = asyncssh.generate_private_key('ssh-ed25519').convert_to_public()
        host_keys.confirm(host_keys.Observation('one.invalid', 22, old))
        dialog = self.dialog([ui.ProbeResult(self.target, self.observation(previous=old)),
                              ui.ProbeResult(target2, self.observation(target2))])
        for row in range(2):
            dialog.table.item(row, 0).setCheckState(QtCore.Qt.CheckState.Checked)
        with mock.patch.object(ui.PlainMessageBox, 'warning', return_value=ui.PlainMessageBox.StandardButton.Cancel):
            dialog._trust()
        self.assertEqual(host_keys.load(), {self.target.endpoint: old, target2.endpoint: self.key})
        dialog.table.setCurrentCell(0, 1)
        self.assertIn(self.key.get_fingerprint('sha256'), dialog.details.toPlainText())
        self.assertIn(old.get_fingerprint('sha256'), dialog.details.toPlainText())

    def test_timeout_protocol_and_dns_errors_each_continue(self):
        targets = [self.target] * 4
        worker = ui._ProbeWorker(targets)
        with mock.patch.object(ssh_connection, 'inspect_host', new=mock.AsyncMock(side_effect=[
                TimeoutError(), asyncssh.ProtocolError('invalid SSH'), OSError('DNS failed'), self.observation()])):
            worker.run()
        self.assertEqual(len(worker.results), 4)
        self.assertTrue(all(r.error for r in worker.results[:3]))
        self.assertIsNotNone(worker.results[3].observation)
        self.assertEqual(host_keys.load(), {})

    def test_readers_wait_until_pair_write_finishes(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        writing = Event()
        release = Event()
        reader_started = Event()
        original = host_keys._write_truststore_mac
        def paused_mac(raw):
            writing.set()
            if not release.wait(5):
                raise AssertionError('writer release missing')
            original(raw)
        def read():
            reader_started.set()
            return host_keys.load()
        with ThreadPoolExecutor(max_workers=2) as pool:
            with mock.patch.object(host_keys, '_write_truststore_mac', side_effect=paused_mac):
                writer = pool.submit(host_keys.confirm, self.observation())
                try:
                    self.assertTrue(writing.wait(5))
                    reader = pool.submit(read)
                    self.assertTrue(reader_started.wait(5))
                    self.assertFalse(reader.done())
                finally:
                    release.set()
                writer.result(timeout=5)
                self.assertEqual(reader.result(timeout=5), {self.target.endpoint: self.key})


class TrustTransportTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def test_inspection_ignores_config_and_never_authenticates(self):
        events = []
        class Server(asyncssh.SSHServer):
            def begin_auth(self, user):
                events.append('auth'); return True
        key = asyncssh.generate_private_key('ssh-ed25519')
        server = await asyncssh.create_server(Server,'127.0.0.1',0,server_host_keys=[key])
        try:
            port = server.sockets[0].getsockname()[1]
            config = self.root/'malicious-config'
            config.write_text('Host *\n HostName bad.invalid\n HostKeyAlias bad\n Port 1\n User bad\n'
                              ' ProxyCommand nonexistent-command\n ProxyJump bad\n IdentityFile /missing\n CertificateFile /missing\n')
            def no_client_keys(keys, *args, **kwargs):
                # AsyncSSH calls load_keypairs(None) for disabled host-based auth.
                # Empty input is harmless; any actual identity would fail this test.
                self.assertFalse(keys, 'inspection tried to load a client identity')
                return []
            with mock.patch('os.path.expanduser',return_value=str(config)), \
                    mock.patch.object(ssh_connection,'auth_params',side_effect=AssertionError('auth params')), \
                    mock.patch.object(crypto,'decrypt_host_password',side_effect=AssertionError('decrypt')), \
                    mock.patch('asyncssh.connection.SSHAgentClient',side_effect=AssertionError('agent')), \
                    mock.patch('asyncssh.connection.load_keypairs',side_effect=no_client_keys), \
                    mock.patch('asyncssh.connection.load_default_keypairs',side_effect=AssertionError('default client key')), \
                    mock.patch.object(asyncio.get_running_loop(),'subprocess_exec',side_effect=AssertionError('proxy')):
                observation = await ssh_connection.inspect_host(dict(self.host,primary_ip='127.0.0.1',port=port,key_path='/missing'))
            self.assertEqual(observation.key,key.convert_to_public())
            self.assertEqual(events,[])
            self.assertEqual(host_keys.load(),{})
        finally:
            server.close(); await server.wait_closed()

    async def test_rejected_password_and_keyboard_interactive_are_bounded(self):
        attempts = []
        class Server(asyncssh.SSHServer):
            def begin_auth(self,user): return True
            def password_auth_supported(self): return True
            def validate_password(self,user,password):
                attempts.append(('password',password)); return False
            def kbdint_auth_supported(self): return True
            def get_kbdint_challenge(self,user,lang,submethods):
                return ('','','',[('Password:',False)])
            def validate_kbdint_response(self,user,responses):
                attempts.append(('kbdint',responses)); return False
        key = asyncssh.generate_private_key('ssh-ed25519')
        server = await asyncssh.create_server(Server,'127.0.0.1',0,server_host_keys=[key])
        try:
            port=server.sockets[0].getsockname()[1]
            hid=db.add_or_update_host(proxmox_uid=None,name='loopback',primary_ip='127.0.0.1',port=port,
                                      user='root',auth_method='password',password_plain='SYNTHETIC-SECRET')
            host_keys.confirm(host_keys.Observation('127.0.0.1',port,key.convert_to_public()))
            config = self.root/'ignored-config'
            config.write_text('Host *\n HostName bad.invalid\n HostKeyAlias bad\n Port 1\n User bad\n'
                              ' ProxyCommand nonexistent-command\n ProxyJump bad\n', encoding='utf-8')
            with self.assertRaises(asyncssh.PermissionDenied), \
                    mock.patch('os.path.expanduser', return_value=str(config)), \
                    mock.patch.object(asyncio.get_running_loop(), 'subprocess_exec', side_effect=AssertionError('proxy')):
                async with ssh_connection.connect_host(db.get_host(hid)):
                    self.fail('Rejected password accepted')
            self.assertEqual(attempts,[('password','SYNTHETIC-SECRET'),('kbdint',['SYNTHETIC-SECRET'])])
        finally:
            server.close(); await server.wait_closed()
