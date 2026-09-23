"""Synthetic-only credential, snapshot, unlock and confirmation regressions."""
import asyncio
from contextlib import asynccontextmanager, closing, contextmanager
import io
import json
import logging
from pathlib import Path
import sqlite3
import sys
import tempfile
import traceback
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PyQt6 import QtWidgets
from sshupdater.core import credentials, crypto, db, settings, ssh_connection, host_keys
from sshupdater.ui_config import LegacyPasswordDialog, confirm_legacy_passwords


class CredentialV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for module, name, value in (
            (db, 'DB_PATH', self.root / 'app.db'),
            (crypto, 'DATA_DIR', self.root), (crypto, '_FERNET', None),
            (crypto, '_SALT_PATH', self.root / 'vault.salt'),
            (crypto, '_VERIFIER_PATH', self.root / 'vault.verify'),
            (settings, 'DATA_DIR', self.root), (settings, 'KNOWN_HOSTS', self.root / 'known_hosts'),
        ):
            patch = mock.patch.object(module, name, value)
            patch.start(); self.addCleanup(patch.stop)
        crypto.set_master_password('synthetic master')
        db.init_db()
        self.values = dict(name='sample', primary_ip='alias.example', user='admin', port=22,
                           auth_method='password')
        self.hid = db.add_or_update_host(proxmox_uid=None, password_plain='synthetic secret', **self.values)
        self.host = db.get_host(self.hid)

    def raw_token(self, token, hid=None):
        with closing(db._connect()) as con, con:
            con.execute('UPDATE hosts SET password_enc=? WHERE id=?',
                        (token, self.hid if hid is None else hid))

    def legacy(self, hid=None):
        token = crypto.encrypt_str('synthetic legacy secret')
        self.raw_token(token, hid)
        return token

    def payload(self):
        return dict(version=2, purpose='ssh-password', host_id=self.hid,
                    primary_ip='alias.example', user='admin', port=22, secret='synthetic secret')

    def encode_payload(self, payload):
        return credentials.V2_PREFIX + crypto._FERNET.encrypt(
            credentials.MAGIC + json.dumps(payload).encode())

    def assert_unlock_unchanged_failure(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        with self.assertRaisesRegex(OSError, 'inkonsistent'):
            crypto.set_master_password('synthetic master')
        self.assertFalse(crypto.is_unlocked())
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})

    def test_v2_roundtrip_and_authenticated_payload(self):
        self.assertEqual(credentials.version(self.host['password_enc']), 2)
        self.assertEqual(db.get_host_password(self.hid), 'synthetic secret')
        plain = crypto._FERNET.decrypt(self.host['password_enc'][len(credentials.V2_PREFIX):])
        self.assertTrue(plain.startswith(credentials.MAGIC))
        self.assertEqual(json.loads(plain[len(credentials.MAGIC):]), self.payload())

    def test_token_cannot_move_to_another_row_even_with_same_target(self):
        other = db.add_or_update_host(proxmox_uid=None, **dict(self.values, name='other'))
        self.raw_token(self.host['password_enc'], other)
        with self.assertRaisesRegex(credentials.CredentialError, 'Hostidentität'):
            db.get_host_password(other)

    def test_each_identity_mismatch_is_rejected(self):
        for field, value in [('id', 999), ('primary_ip', '192.0.2.3'), ('user', 'root'), ('port', 2222)]:
            with self.subTest(field=field), self.assertRaises(credentials.CredentialError):
                crypto.decrypt_host_password(self.host['password_enc'], dict(self.host, **{field: value}))

    def test_defaults_and_port_normalization_are_shared(self):
        source = dict(id=9, primary_ip='SSH-Alias', user=None, port=None)
        token = crypto.encrypt_host_password('synthetic', source)
        for user, port in [('root', 22), ('', '22'), (None, '022')]:
            target = dict(source, user=user, port=port)
            self.assertEqual(crypto.decrypt_host_password(token, target), 'synthetic')
            self.assertEqual(db.connection_context(target)['port'], 22)
        self.assertFalse(db.target_changed(source, dict(source, user='root', port='22')))
        for host in ['ssh-alias', '192.0.2.1']:
            with self.assertRaises(credentials.CredentialError):
                crypto.decrypt_host_password(token, dict(source, primary_ip=host))

    def test_invalid_ports_are_never_coerced_or_defaulted(self):
        for port in [True, False, 0, -1, 65536, 22.0, '22.0', '', ' 22', '+22', 'secret']:
            with self.subTest(port=port):
                for operation in [lambda h: crypto.encrypt_host_password('synthetic', h),
                                  db.connection_context,
                                  lambda h: db.target_changed(self.host, h)]:
                    with self.assertRaises(credentials.CredentialError):
                        operation(dict(self.host, port=port))

    def test_strict_payload_schema_and_types(self):
        variants = [[], {}, dict(self.payload(), extra='x')]
        for field in self.payload():
            value = dict(self.payload()); del value[field]; variants.append(value)
        for field, value in [('version', True), ('version', 2.0), ('version', '2'),
                             ('purpose', 'other'), ('host_id', True), ('host_id', 0),
                             ('primary_ip', None), ('user', []), ('port', '22'),
                             ('port', True), ('port', 22.0), ('secret', 123), ('secret', '')]:
            variants.append(dict(self.payload(), **{field: value}))
        for payload in variants:
            with self.subTest(payload_type=type(payload).__name__), self.assertRaises(credentials.CredentialError):
                crypto.decrypt_host_password(self.encode_payload(payload), self.host)

    def test_duplicate_json_fields_and_wrong_magic_rejected(self):
        for plain in [credentials.MAGIC + b'{"version":2,"version":2}', b'wrong magic',
                      credentials.MAGIC + b'not json', credentials.MAGIC + b'\xff']:
            token = credentials.V2_PREFIX + crypto._FERNET.encrypt(plain)
            with self.assertRaises(credentials.CredentialError):
                crypto.decrypt_host_password(token, self.host)

    def test_corrupted_token_and_unknown_versions_fail_closed(self):
        for token in [credentials.V2_PREFIX + b'broken',
                      self.host['password_enc'][:-10] + b'xxxxxxxxxx',
                      credentials.PREFIX + b'v3:' + b'anything',
                      self.encode_payload(dict(self.payload(), version=3))]:
            with self.subTest(prefix=token[:24]), self.assertRaises(credentials.CredentialError):
                crypto.decrypt_host_password(token, self.host)

    def test_recognized_v2_never_falls_back_to_legacy(self):
        f = crypto._FERNET
        cases = [credentials.V2_PREFIX + b'broken',
                 credentials.V2_PREFIX + f.encrypt(b'wrong marker'),
                 self.encode_payload(dict(self.payload(), version=3)),
                 self.encode_payload(dict(self.payload(), purpose='other')),
                 self.encode_payload(dict(self.payload(), host_id=999)),
                 self.encode_payload(dict(self.payload(), port='22')),
                 credentials.V2_PREFIX + f.encrypt(credentials.MAGIC + b'not json')]
        original = credentials._decrypt_legacy
        with mock.patch.object(credentials, '_decrypt_legacy', wraps=original) as legacy:
            # Positive control: this really is the V1 code path.
            credentials.decrypt(f, f.encrypt(b'synthetic legacy'), self.host, validate_legacy=True)
            legacy.assert_called_once()
            legacy.reset_mock()
            for token in cases:
                for validate_legacy in (False, True):
                    with self.subTest(case=cases.index(token), unlock=validate_legacy):
                        with self.assertRaises(credentials.CredentialError) as error:
                            credentials.decrypt(f, token, self.host, validate_legacy=validate_legacy)
                        self.assertNotIsInstance(error.exception, credentials.LegacyCredentialRequired)
                        legacy.assert_not_called()
        stripped = self.host['password_enc'][len(credentials.V2_PREFIX):]
        with self.assertRaises(credentials.CredentialError):
            credentials.decrypt(f, stripped, self.host, validate_legacy=True)

    def test_rename_tags_and_check_results_preserve_token_and_validity(self):
        db.update_host(self.hid, **dict(self.values, name='renamed'))
        db.add_or_update_host(proxmox_uid=None, **dict(self.values, name='renamed'), tags=['changed'])
        db.set_check_result(self.hid, 'synthetic time', 7)
        self.assertEqual(db.get_host(self.hid)['password_enc'], self.host['password_enc'])
        self.assertEqual(db.get_host_password(self.hid), 'synthetic secret')
        self.assertEqual(db.get_connection_context(self.host), db.connection_context(self.host))

    def test_target_changes_require_reentry_or_explicit_delete(self):
        for field, value in [('primary_ip', 'other'), ('user', 'other'), ('port', 2222)]:
            with self.subTest(field=field):
                changed = dict(self.values, **{field: value})
                with self.assertRaises(db.CredentialChangeRequired):
                    db.update_host(self.hid, **changed)
                self.assertEqual(db.get_host(self.hid), self.host)
                db.update_host(self.hid, password_plain='replacement', **changed)
                self.assertEqual(db.get_host_password(self.hid), 'replacement')
                db.update_host(self.hid, password_plain='synthetic secret', **self.values)
                self.host = db.get_host(self.hid)
        db.update_host(self.hid, delete_password=True, **dict(self.values, port=23))
        self.assertIsNone(db.get_host_password(self.hid))

    def test_new_host_has_id_before_encrypt_and_failure_rolls_back(self):
        observed = []
        def fail(secret, host):
            self.assertGreater(host['id'], self.hid)
            observed.append(host['id'])
            raise credentials.CredentialError('synthetic encryption failure')
        with mock.patch.object(crypto, 'encrypt_host_password', side_effect=fail):
            with self.assertRaises(credentials.CredentialError):
                db.add_or_update_host(proxmox_uid=None, password_plain='new',
                                      **dict(self.values, name='new'))
        self.assertEqual(len(observed), 1)
        self.assertEqual(db.list_hosts(), [self.host])
        # Also proves the failed transaction released its lock.
        db.set_host_password(self.hid, 'after rollback')

    def test_failed_replacement_rolls_back_host_and_credential(self):
        with mock.patch.object(crypto, 'encrypt_host_password', side_effect=OSError('failure')):
            with self.assertRaises(OSError):
                db.update_host(self.hid, password_plain='new', **dict(self.values, primary_ip='new'))
        self.assertEqual(db.get_host(self.hid), self.host)

    def test_set_host_password_uses_v2_and_rejects_missing_host(self):
        db.set_host_password(self.hid, 'replacement')
        self.assertEqual(credentials.version(db.get_host(self.hid)['password_enc']), 2)
        self.assertEqual(db.get_host_password(self.hid), 'replacement')
        with self.assertRaises(db.HostNotFoundError):
            db.set_host_password(999, 'synthetic')

    def test_mixed_unlock_preserves_all_existing_files(self):
        other = db.add_or_update_host(proxmox_uid=None, **dict(self.values, name='legacy'))
        token = self.legacy(other)
        (self.root / 'config.enc').write_bytes(crypto.encrypt_str('synthetic config'))
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        crypto.set_master_password('synthetic master')
        self.assertTrue(crypto.is_unlocked())
        self.assertEqual(db.get_host(other)['password_enc'], token)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        self.assertEqual(db.get_host_password(self.hid), 'synthetic secret')

    def test_unlock_rejects_binding_mismatch_without_mutation(self):
        self.raw_token(self.encode_payload(dict(self.payload(), host_id=999)))
        self.assert_unlock_unchanged_failure()

    def test_unlock_rejects_corrupt_v2_without_mutation(self):
        self.raw_token(credentials.V2_PREFIX + b'broken')
        self.assert_unlock_unchanged_failure()

    def test_unlock_rejects_unknown_outer_version_without_mutation(self):
        self.raw_token(credentials.PREFIX + b'v99:' + self.host['password_enc'])
        self.assert_unlock_unchanged_failure()

    def test_unlock_rejects_unknown_inner_version_without_mutation(self):
        self.raw_token(self.encode_payload(dict(self.payload(), version=99)))
        self.assert_unlock_unchanged_failure()

    def test_legacy_cannot_be_used_for_new_ssh_authentication(self):
        token = self.legacy()
        async def attempt():
            with self.assertRaises(credentials.LegacyCredentialRequired):
                async with ssh_connection.connect_host(db.get_host(self.hid)):
                    self.fail('connected')
        with mock.patch.object(ssh_connection.asyncssh, 'connect') as connect:
            asyncio.run(attempt())
        connect.assert_not_called()
        self.assertEqual(db.get_host(self.hid)['password_enc'], token)

    def test_key_host_with_legacy_password_does_not_decrypt_or_prompt(self):
        token = self.legacy()
        db.update_host(self.hid, keep_password=True, **dict(self.values, auth_method='key'))
        host = db.get_host(self.hid)
        with mock.patch.object(crypto, 'decrypt_host_password', side_effect=AssertionError('decrypt')), \
                mock.patch.object(LegacyPasswordDialog, 'exec', side_effect=AssertionError('prompt')):
            self.assertTrue(confirm_legacy_passwords(None, [self.hid]))
            options = ssh_connection.options_for(db.get_connection_context(host), config=[])
        self.assertFalse(options.password_auth)
        self.assertFalse(options.kbdint_auth)
        self.assertEqual(db.get_host(self.hid)['password_enc'], token)

    def test_confirmation_is_empty_and_changes_only_reviewed_host(self):
        self.legacy()
        other = db.add_or_update_host(proxmox_uid=None, **dict(self.values, name='other'))
        other_token = self.legacy(other)
        dialog = LegacyPasswordDialog(None, db.get_host(self.hid))
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.password.text(), '')
        self.assertEqual(dialog.password.echoMode(), QtWidgets.QLineEdit.EchoMode.Password)
        labels = [label.text() for label in dialog.findChildren(QtWidgets.QLabel)]
        for expected in ['sample', 'alias.example', 'admin', '22']:
            self.assertIn(expected, labels)
        dialog._save()  # Empty input never confirms.
        self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Rejected)
        dialog.password.setText('explicit synthetic replacement')
        dialog._save()
        self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.password.text(), '')
        self.assertEqual(db.get_host_password(self.hid), 'explicit synthetic replacement')
        self.assertEqual(db.get_host(other)['password_enc'], other_token)

    def test_cancel_preserves_legacy_and_prevents_action(self):
        token = self.legacy()
        def cancel(dialog):
            dialog.password.setText('discarded synthetic input')
            dialog.reject()
            self.assertEqual(dialog.password.text(), '')
            return dialog.result()
        with mock.patch.object(LegacyPasswordDialog, 'exec', cancel):
            self.assertFalse(confirm_legacy_passwords(None, [self.hid]))
        self.assertEqual(db.get_host(self.hid)['password_enc'], token)

    def test_confirmation_rejects_concurrent_host_or_password_change(self):
        for change in ['target', 'password']:
            with self.subTest(change=change):
                self.legacy()
                dialog = LegacyPasswordDialog(None, db.get_host(self.hid))
                if change == 'target':
                    db.update_host(self.hid, password_plain='concurrent', **dict(self.values, port=2222))
                else:
                    db.set_host_password(self.hid, 'concurrent')
                before = db.get_host(self.hid)
                dialog.password.setText('stale confirmation')
                with mock.patch('sshupdater.ui_config.QMessageBox.warning') as warning:
                    dialog._save()
                warning.assert_called_once()
                self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Rejected)
                self.assertEqual(db.get_host(self.hid), before)
                dialog.close()
                db.update_host(self.hid, password_plain='synthetic secret', **self.values)

    def test_connection_context_is_immutable_and_read_in_one_select(self):
        original = db._connect
        queries = []
        def connect():
            con = original(); con.set_trace_callback(queries.append); return con
        with mock.patch.object(db, '_connect', side_effect=connect):
            context = db.get_connection_context(self.host)
        self.assertEqual(sum(q.startswith('SELECT') for q in queries), 1)
        with self.assertRaises(TypeError):
            context['port'] = 99
        self.assertEqual(context['password_enc'], self.host['password_enc'])
        self.assertEqual(ssh_connection.auth_params(context)['password'], 'synthetic secret')

    def test_isolated_password_mode_snapshot_changes(self):
        for field, value in [('primary_ip', 'other'), ('user', 'other'), ('port', 2222),
                             ('auth_method', 'key'),
                             ('password_enc', crypto.encrypt_host_password('replacement', self.host))]:
            with self.subTest(field=field):
                self.raw_field(field, value)
                current = db.get_host(self.hid)
                self.assertEqual({k for k in current if current[k] != self.host[k]}, {field})
                async def attempt():
                    with self.assertRaises(db.HostConfigurationChanged):
                        async with ssh_connection.connect_host(self.host):
                            self.fail('connected')
                with mock.patch.object(ssh_connection.asyncssh, 'connect') as connect:
                    asyncio.run(attempt())
                connect.assert_not_called()
                self.raw_field(field, self.host[field])
        # An unused key path must not change password authentication either.
        self.raw_field('key_path', '/synthetic/unused')
        self.assertEqual(db.get_connection_context(self.host), db.connection_context(self.host))

    def test_deleted_host_is_not_connected(self):
        with closing(db._connect()) as con, con:
            con.execute('DELETE FROM hosts WHERE id=?', (self.hid,))
        with self.assertRaises(db.HostConfigurationChanged):
            db.get_connection_context(self.host)

    def test_connection_uses_validated_snapshot_without_later_db_reads(self):
        captured = []
        @asynccontextmanager
        async def opened(options, requested, **kwargs):
            db.update_host(self.hid, password_plain='new password', **dict(self.values, port=2222))
            captured.append((options.host, options.port, options.username, options.password, requested))
            yield object()
        async def attempt():
            async with ssh_connection.connect_host(self.host):
                pass
        original = ssh_connection.options_for
        with mock.patch.object(ssh_connection, '_open', opened), \
                mock.patch.object(ssh_connection, 'options_for', side_effect=lambda h: original(h, config=[])), \
                mock.patch.object(db, 'get_host_password', side_effect=AssertionError('late password read')):
            asyncio.run(attempt())
        self.assertEqual(captured, [('alias.example', 22, 'admin', 'synthetic secret', 'alias.example')])

    def test_errors_and_tracebacks_never_include_plaintext_payload(self):
        marker = 'SYNTHETIC-SECRET-DO-NOT-LOG'
        malformed = credentials.V2_PREFIX + crypto._FERNET.encrypt(credentials.MAGIC + marker.encode())
        with self.assertNoLogs(level=logging.DEBUG):
            try:
                crypto.decrypt_host_password(malformed, self.host)
            except credentials.CredentialError as exc:
                formatted = ''.join(traceback.format_exception(exc))
                self.assertNotIn(marker, formatted)
                self.assertNotIn('JSONDecodeError', formatted)
            else:
                self.fail('malformed payload accepted')

    def test_database_handles_close_for_replace_and_rename(self):
        db.get_connection_context(self.host)
        db.set_host_password(self.hid, 'synthetic replacement')
        crypto.set_master_password('synthetic master')
        # Also runs on Windows CI, where outstanding handles prevent rename/delete.
        moved = db.DB_PATH.with_name('moved.db')
        db.DB_PATH.rename(moved)
        moved.rename(db.DB_PATH)
        self.assertEqual(db.get_host_password(self.hid), 'synthetic replacement')

    def test_real_ssh_password_login_with_v2_and_pinned_server(self):
        import asyncssh
        received = []
        class Server(asyncssh.SSHServer):
            def begin_auth(self, username):
                return True
            def password_auth_supported(self):
                return True
            def validate_password(self, username, password):
                received.append((username, password))
                return username == 'admin' and password == 'synthetic secret'
        async def run():
            key = asyncssh.generate_private_key('ssh-ed25519')
            server = await asyncssh.create_server(Server, '127.0.0.1', 0, server_host_keys=[key])
            try:
                port = server.get_port()
                db.update_host(self.hid, password_plain='synthetic secret',
                               **dict(self.values, primary_ip='127.0.0.1', port=port))
                host_keys.confirm(host_keys.Observation('127.0.0.1', port, key.convert_to_public()))
                original = ssh_connection.options_for
                with mock.patch.object(ssh_connection, 'options_for', side_effect=lambda h: original(h, config=[])):
                    async with ssh_connection.connect_host(db.get_host(self.hid)):
                        pass
            finally:
                server.close(); await server.wait_closed()
        asyncio.run(run())
        self.assertEqual(received, [('admin', 'synthetic secret')])

    def test_every_gui_action_stops_before_worker_on_legacy_cancel(self):
        from sshupdater import ui_main
        self.legacy()
        cases = [('_on_check', '_CheckWorker'), ('_on_sim', '_SimWorker'),
                 ('_on_upgrade', '_UpgradeWorker'), ('_on_clean', '_CleanSimWorker'),
                 ('_on_clean_sim_done', '_CleanRunWorker'), ('_on_reboot', '_RebootWorker')]
        for slot, worker_class in cases:
            with self.subTest(action=slot), mock.patch.object(ui_main.SysInfoWidget, 'refresh'):
                window = ui_main.MainWindow()
                window._clean_selected = [self.hid]
                window._clean_results = {self.hid: 'ok'}
                from types import SimpleNamespace
                window.clean_sim_worker = SimpleNamespace(fatal_error=None, stop_requested=False)
                with mock.patch.object(window, '_get_selected_host_ids', return_value=[self.hid]), \
                        mock.patch.object(LegacyPasswordDialog, 'exec', return_value=QtWidgets.QDialog.DialogCode.Rejected), \
                        mock.patch.object(ui_main.PlainMessageBox, 'question', return_value=ui_main.PlainMessageBox.StandardButton.Yes), \
                        mock.patch.object(ui_main, worker_class) as worker:
                    getattr(window, slot)()
                worker.assert_not_called()
                self.assertTrue(window.act_config.isEnabled())
                self.assertFalse(window.act_stop.isEnabled())
                window.close()

    def test_every_worker_blocks_stale_target_before_connecting(self):
        from sshupdater import ui_main
        # Change only the port, keeping the credential exactly unchanged.
        self.raw_field('port', 2222)
        self.assertEqual(db.get_host(self.hid)['password_enc'], self.host['password_enc'])
        for worker_cls, signal in [(ui_main._CheckWorker, 'one_result'),
                                   (ui_main._SimWorker, 'one_result'),
                                   (ui_main._UpgradeWorker, 'host_done'),
                                   (ui_main._CleanSimWorker, 'one_result'),
                                   (ui_main._CleanRunWorker, 'host_done'),
                                   (ui_main._RebootWorker, 'host_done')]:
            with self.subTest(worker=worker_cls.__name__):
                worker = worker_cls([self.hid])
                results = []
                getattr(worker, signal).connect(results.append)
                with mock.patch.object(db, 'list_hosts', return_value=[self.host]), \
                        mock.patch.object(ssh_connection.asyncssh, 'connect') as connect:
                    worker.run()
                connect.assert_not_called()
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]['status'], 'error')
                self.assertIn('zwischenzeitlich geändert', results[0]['note'])

    def test_v122_v123_schema_and_legacy_vault_remain_readable_without_migration(self):
        # This schema is identical in the v1.2.2 and v1.2.3 source tags.
        schema = (Path(__file__).parent / 'fixtures' / 'v1.2.2-v1.2.3-schema.sql').read_text(encoding='utf-8')
        for old_version in ('1.2.2', '1.2.3'):
            with self.subTest(version=old_version):
                root = self.root / old_version
                root.mkdir()
                salt = crypto._SALT_PATH.read_bytes()
                verifier = crypto._VERIFIER_PATH.read_bytes()
                (root / 'vault.salt').write_bytes(salt)
                (root / 'vault.verify').write_bytes(verifier)
                legacy = crypto.encrypt_str('old synthetic password')
                path = root / 'app.db'
                with closing(sqlite3.connect(path)) as con, con:
                    con.executescript(schema)
                    con.execute("INSERT INTO hosts(id,name,primary_ip,user,port,auth_method,password_enc) VALUES(1,'old','alias','root',22,'password',?)", (legacy,))
                    con.execute("INSERT INTO hosts(id,name,primary_ip,user,port,auth_method) VALUES(2,'key','alias','root',22,'key')")
                before = {p.name: p.read_bytes() for p in root.iterdir()}
                with mock.patch.object(db, 'DB_PATH', path), mock.patch.object(crypto, 'DATA_DIR', root), \
                        mock.patch.object(crypto, '_SALT_PATH', root / 'vault.salt'), \
                        mock.patch.object(crypto, '_VERIFIER_PATH', root / 'vault.verify'):
                    crypto.set_master_password('synthetic master')
                    db.init_db()
                    self.assertEqual(len(db.list_hosts()), 2)
                    self.assertEqual(db.get_host(1)['password_enc'], legacy)
                    self.assertEqual(db.get_connection_context(db.get_host(2))['auth_method'], 'key')
                    with self.assertRaises(credentials.LegacyCredentialRequired):
                        db.get_host_password(1)
                    self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})

    def test_workers_allow_historical_default_user_and_port(self):
        from sshupdater import ui_main
        from sshupdater.core import ssh_client
        db.update_host(self.hid, password_plain='synthetic secret',
                       **dict(self.values, user=None, port=None))
        host = db.get_host(self.hid)
        context = db.get_connection_context(host)
        options = ssh_connection.options_for(context, config=[])
        self.assertEqual((options.username, options.port), ('root', 22))
        self.assertEqual(options.password, 'synthetic secret')
        for worker_cls, method in [(ui_main._CheckWorker, 'check_updates_for_host'),
                                   (ui_main._SimWorker, 'simulate_upgrade_for_host')]:
            operation = mock.AsyncMock(return_value={'status': 'ok', 'host_id': self.hid})
            with mock.patch.object(ssh_client, method, operation):
                worker_cls([self.hid]).run()
            operation.assert_awaited_once()

    def test_key_host_without_credential_gets_consistent_snapshot(self):
        hid = db.add_or_update_host(proxmox_uid=None, name='key-only', primary_ip='SSH-Alias',
                                    user=None, port=None, auth_method='key')
        host = db.get_host(hid)
        with mock.patch.object(crypto, 'decrypt_host_password', side_effect=AssertionError('decrypt')):
            context = db.get_connection_context(host)
            options = ssh_connection.options_for(context, config=[])
        self.assertIsNone(context['password_enc'])
        self.assertTrue(options.public_key_auth)
        self.assertFalse(options.password_auth)
        self.assertEqual((options.host, options.username, options.port), ('SSH-Alias', 'root', 22))

    def raw_field(self, field, value):
        assert field in {'primary_ip', 'user', 'port', 'auth_method', 'key_path', 'password_enc'}
        with closing(db._connect()) as con, con:
            con.execute(f'UPDATE hosts SET {field}=? WHERE id=?', (value, self.hid))

    def test_ascii_and_unchanged_unicode_usernames_roundtrip_to_ssh(self):
        for username in ('admin', 'jörg', '管理者'):
            with self.subTest(username=username):
                db.update_host(self.hid, password_plain='synthetic secret',
                               **dict(self.values, user=username))
                host = db.get_host(self.hid)
                context = db.get_connection_context(host)
                options = ssh_connection.options_for(context, config=[])
                self.assertEqual(options.username, username)
                self.assertEqual(db.get_host_password(self.hid), 'synthetic secret')

    def test_saslprep_transformations_rejected_without_changing_database(self):
        for username in ('Ａdmin', 'a\u00addmin', 'jo\u0308rg', 'ad\u00a0min'):
            with self.subTest(username=ascii(username)):
                with self.assertRaises(credentials.CredentialError):
                    db.update_host(self.hid, password_plain='synthetic secret',
                                   **dict(self.values, user=username))
                self.assertEqual(db.get_host(self.hid), self.host)
                # Even an authenticated stored token plus matching row must fail.
                token = self.encode_payload(dict(self.payload(), user=username))
                with self.assertRaises(credentials.CredentialError):
                    crypto.decrypt_host_password(token, dict(self.host, user=username))
                with self.assertRaises(credentials.CredentialError):
                    db.connection_context(dict(self.host, user=username, auth_method='key'))

    def test_invalid_usernames_rejected_before_storage_or_use(self):
        for username in ('bad\ud800', 'bad\x00', 'bad\n', 'aא'):
            with self.subTest(username=ascii(username)):
                with self.assertRaises(credentials.CredentialError):
                    db.add_or_update_host(proxmox_uid=None, password_plain='synthetic',
                                          **dict(self.values, name='invalid', user=username))
                self.assertEqual(db.list_hosts(), [self.host])
                token = self.encode_payload(dict(self.payload(), user=username))
                with self.assertRaises(credentials.CredentialError):
                    crypto.decrypt_host_password(token, dict(self.host, user=username))

    def test_valid_ascii_and_unicode_secrets_preserved_without_normalization(self):
        for secret in ('synthetic ASCII', 'päss🔑管理', 'Ａpa\u0308ss\u00adword'):
            with self.subTest(kind='unicode' if not secret.isascii() else 'ascii'):
                db.set_host_password(self.hid, secret)
                host = db.get_host(self.hid)
                self.assertEqual(db.get_host_password(self.hid), secret)
                options = ssh_connection.options_for(db.get_connection_context(host), config=[])
                self.assertEqual(options.password, secret)
                from asyncssh.packet import String
                self.assertEqual(String(options.password)[4:], secret.encode('utf-8'))

    def test_non_utf8_secrets_rejected_on_write_and_read(self):
        for secret in ('synthetic\ud800', '\udfff', '\ud800\udc00'):
            with self.subTest(kind=ascii(secret)):
                with self.assertRaises(credentials.CredentialError):
                    db.set_host_password(self.hid, secret)
                self.assertEqual(db.get_host(self.hid), self.host)
                # Use raw UTF-8 bytes of JSON with an escaped lone surrogate.
                # A surrogate pair in JSON legitimately decodes to a Unicode scalar.
                if secret == '\ud800\udc00':
                    continue
                token = self.encode_payload(dict(self.payload(), secret=secret))
                with self.assertRaises(credentials.CredentialError):
                    crypto.decrypt_host_password(token, self.host)

    def test_complete_payload_rejected_only_for_duplicate_required_field(self):
        payload = json.dumps(self.payload(), separators=(',', ':'))
        duplicated = payload[:-1] + ',"purpose":"ssh-password"}'
        # Removing the duplicate leaves a fully valid payload.
        normal_token = credentials.V2_PREFIX + crypto._FERNET.encrypt(credentials.MAGIC + payload.encode())
        self.assertEqual(crypto.decrypt_host_password(normal_token, self.host), 'synthetic secret')
        token = credentials.V2_PREFIX + crypto._FERNET.encrypt(credentials.MAGIC + duplicated.encode())
        original = credentials._unique_object
        errors = []
        def observe(pairs):
            try:
                return original(pairs)
            except ValueError as exc:
                errors.append(str(exc))
                raise
        with mock.patch.object(credentials, '_unique_object', side_effect=observe):
            with self.assertRaises(credentials.CredentialError):
                crypto.decrypt_host_password(token, self.host)
        self.assertEqual(errors, ['duplicate field'])

    def test_key_snapshot_ignores_absent_v1_v2_and_changed_retained_credentials(self):
        from sshupdater import ui_main
        from sshupdater.core import ssh_client
        self.raw_field('auth_method', 'key')
        variants = (None, crypto.encrypt_str('synthetic legacy'), self.host['password_enc'])
        original_options = ssh_connection.options_for
        for token in variants:
            with self.subTest(format='none' if token is None else credentials.version(token)):
                self.raw_token(token)
                old = db.get_host(self.hid)
                self.assertIsNone(db.connection_context(old)['password_enc'])
                changed = crypto.encrypt_str('synthetic retained replacement')
                self.raw_token(changed)
                self.assertEqual({k for k in old if old[k] != db.get_host(self.hid)[k]}, {'password_enc'})
                self.assertEqual(db.get_connection_context(old), db.connection_context(old))
                calls = []
                @asynccontextmanager
                async def opened(options, requested, **kwargs):
                    calls.append(options)
                    yield object()
                worker = ui_main._CheckWorker([self.hid])
                results = []
                worker.one_result.connect(results.append)
                with mock.patch.object(db, 'list_hosts', return_value=[old]), \
                        mock.patch.object(crypto, 'decrypt_host_password', side_effect=AssertionError('unused credential')), \
                        mock.patch.object(ssh_connection, 'options_for', side_effect=lambda h: original_options(h, config=[])), \
                        mock.patch.object(ssh_connection, '_open', opened), \
                        mock.patch.object(ssh_client, '_detect_distro', new=mock.AsyncMock(return_value='debian')), \
                        mock.patch.object(ssh_client, '_check_debian', new=mock.AsyncMock(return_value=(0, ''))):
                    worker.run()
                self.assertEqual(len(calls), 1)
                self.assertFalse(calls[0].password_auth)
                self.assertEqual(results[0]['status'], 'ok')
                self.assertEqual(db.get_host(self.hid)['password_enc'], changed)

    def test_key_snapshot_rejects_each_isolated_relevant_change(self):
        self.raw_field('auth_method', 'key')
        old = db.get_host(self.hid)
        for field, value in [('primary_ip', 'other'), ('user', 'other'), ('port', 2222),
                             ('auth_method', 'password'), ('key_path', '/synthetic/new-key')]:
            with self.subTest(field=field):
                self.raw_field(field, value)
                current = db.get_host(self.hid)
                self.assertEqual({k for k in old if current[k] != old[k]}, {field})
                async def attempt():
                    with self.assertRaises(db.HostConfigurationChanged):
                        async with ssh_connection.connect_host(old):
                            self.fail('connected')
                with mock.patch.object(ssh_connection.asyncssh, 'connect') as connect:
                    asyncio.run(attempt())
                connect.assert_not_called()
                self.raw_field(field, old[field])

    def test_credential_edit_still_rejects_changed_retained_password_on_key_host(self):
        self.raw_field('auth_method', 'key')
        old = db.get_host(self.hid)
        self.legacy()
        current = db.get_host(self.hid)
        with self.assertRaises(db.HostConfigurationChanged):
            db.set_host_password(self.hid, 'stale synthetic replacement', expected=old)
        self.assertEqual(db.get_host(self.hid), current)

    def test_sql_and_commit_failures_after_encryption_roll_back_all_writers(self):
        real_connect = db._connect
        real_encrypt = crypto.encrypt_host_password
        for operation in ('insert', 'update', 'password'):
            for failure in ('sql', 'commit'):
                with self.subTest(operation=operation, failure=failure):
                    observed = []
                    def encrypt(*args):
                        token = real_encrypt(*args)
                        observed.append(token)
                        return token
                    class FaultConnection:
                        def __init__(self):
                            self.con = real_connect()
                        def execute(self, sql, args=()):
                            if failure == 'sql' and sql.startswith('UPDATE hosts SET'):
                                assert observed, 'failure must follow encryption'
                                raise sqlite3.OperationalError('synthetic SQL failure')
                            return self.con.execute(sql, args)
                        def commit(self):
                            assert observed, 'failure must follow encryption'
                            raise sqlite3.OperationalError('synthetic commit failure')
                        def close(self):
                            self.con.close()
                    with mock.patch.object(db, '_connect', side_effect=FaultConnection), \
                            mock.patch.object(crypto, 'encrypt_host_password', side_effect=encrypt):
                        with self.assertRaises(sqlite3.OperationalError):
                            if operation == 'insert':
                                db.add_or_update_host(proxmox_uid=None, password_plain='new',
                                                      **dict(self.values, name='new'))
                            elif operation == 'update':
                                db.update_host(self.hid, password_plain='new', **dict(self.values, port=2222))
                            else:
                                db.set_host_password(self.hid, 'new')
                    self.assertEqual(len(observed), 1)
                    self.assertEqual(credentials.version(observed[0]), 2)
                    self.assertEqual(db.list_hosts(), [self.host])
                    self.assertEqual(db.get_host_password(self.hid), 'synthetic secret')

    def leak_cases(self):
        marker = 'SYNTHETIC-SECRET-NEVER-DISPLAY'
        payload = dict(self.payload(), secret=marker)
        variants = [json.dumps(dict(payload, purpose='wrong')).encode(),
                    json.dumps(dict(payload, host_id=999)).encode(),
                    json.dumps(dict(payload, secret=marker + '\ud800')).encode(),
                    b'{"secret":"' + marker.encode() + b'",BROKEN}']
        return [(credentials.V2_PREFIX + crypto._FERNET.encrypt(credentials.MAGIC + raw),
                 marker, raw.decode('utf-8')) for raw in variants]

    def assert_no_secret_output(self, text, marker, payload):
        self.assertNotIn(marker, text)
        self.assertNotIn(payload, text)
        self.assertNotIn('SSHUPD-CRED', text)
        self.assertNotIn('JSONDecodeError', text)
        self.assertNotIn('UnicodeEncodeError', text)

    def test_unlock_errors_and_application_dialog_do_not_expose_payload(self):
        from sshupdater import app as application
        f = crypto._FERNET
        for token, marker, payload in self.leak_cases():
            with self.subTest(case=payload[:20]):
                self.raw_token(token)
                with self.assertNoLogs(level=logging.DEBUG):
                    with self.assertRaises(OSError) as error:
                        crypto.set_master_password('synthetic master')
                    self.assert_no_secret_output(''.join(traceback.format_exception(error.exception)), marker, payload)
                    # Exercise the real startup error-to-dialog propagation.
                    with mock.patch.object(application.settings, 'initialize'), \
                            mock.patch.object(application.QtWidgets, 'QApplication', return_value=self.app), \
                            mock.patch.object(application.QInputDialog, 'getText', return_value=('synthetic master', True)), \
                            mock.patch.object(application.QMessageBox, 'critical') as message:
                        self.assertEqual(application.main(), 1)
                    message.assert_called_once()
                    self.assert_no_secret_output(str(message.call_args), marker, payload)
                self.assertFalse(crypto.is_unlocked())
                self.assertEqual(db.get_host(self.hid)['password_enc'], token)
                crypto._FERNET = f

    def test_credential_errors_through_every_worker_and_gui_never_expose_payload(self):
        from sshupdater import ui_main
        cases = [(ui_main._CheckWorker, 'one_result', '_on_check_result'),
                 (ui_main._SimWorker, 'one_result', '_on_sim_result'),
                 (ui_main._UpgradeWorker, 'host_done', '_on_upgrade_host_done'),
                 (ui_main._CleanSimWorker, 'one_result', '_on_clean_sim_result'),
                 (ui_main._CleanRunWorker, 'host_done', '_on_clean_host_done'),
                 (ui_main._RebootWorker, 'host_done', '_on_reboot_host_done')]
        with mock.patch.object(ui_main.SysInfoWidget, 'refresh'):
            window = ui_main.MainWindow()
        self.addCleanup(window.close)
        window._clean_results = {}
        for token, marker, payload in self.leak_cases():
            self.raw_token(token)
            for worker_cls, signal, slot in cases:
                with self.subTest(worker=worker_cls.__name__, case=payload[:20]):
                    window.log.clear()
                    worker = worker_cls([self.hid])
                    results = []
                    getattr(worker, signal).connect(results.append)
                    getattr(worker, signal).connect(getattr(window, slot))
                    with self.capture_logs() as logs, \
                            mock.patch.object(ssh_connection.asyncssh, 'connect') as connect:
                        worker.run()
                    connect.assert_not_called()
                    self.assertEqual(len(results), 1)
                    self.assertEqual(results[0]['status'], 'error')
                    self.assertIsNone(worker.fatal_error)
                    self.assertTrue(window.log.toPlainText())
                    self.assert_no_secret_output(str(results) + window.log.toPlainText() + logs.getvalue(), marker, payload)

    def test_gui_save_reports_non_utf8_secret_without_echo_or_mutation(self):
        token = self.legacy()
        dialog = LegacyPasswordDialog(None, db.get_host(self.hid))
        self.addCleanup(dialog.close)
        marker = 'SYNTHETIC-SECRET-NEVER-DISPLAY'
        with mock.patch.object(dialog.password, 'text', return_value=marker + '\ud800'), \
                mock.patch('sshupdater.ui_config.QMessageBox.warning') as message, \
                self.assertNoLogs(level=logging.DEBUG):
            dialog._save()
        message.assert_called_once()
        self.assert_no_secret_output(str(message.call_args), marker, marker + '\ud800')
        self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Rejected)
        self.assertEqual(db.get_host(self.hid)['password_enc'], token)

    @contextmanager
    def capture_logs(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            yield stream
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)
            handler.close()
