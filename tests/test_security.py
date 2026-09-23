from contextlib import closing
import asyncio
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import asyncssh
from cryptography.fernet import Fernet
from sshupdater.core import crypto, db, host_keys, settings, ssh_connection, storage, credentials



def ssh_config_path(path):
    """OpenSSH config uses shell-like quoting, including on Windows."""
    return '"' + path.as_posix().replace('\\', '\\\\').replace('"', '\\"') + '"'


class PrivateStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'data'

    @unittest.skipUnless(os.name == 'posix', 'POSIX permissions')
    def test_new_and_existing_permissions_preserve_contents(self):
        storage.initialize(self.root)
        path = self.root / 'vault.salt'
        storage.write_private(path, b'unchanged', exclusive=True)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.root.chmod(0o755)
        path.chmod(0o644)
        storage.initialize(self.root)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.read_bytes(), b'unchanged')

    def test_file_and_directory_symlinks_rejected(self):
        target = Path(self.temp.name) / 'target'
        target.mkdir()
        self.root.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            storage.initialize(self.root)
        self.root.unlink()
        self.root.mkdir()
        outside = target / 'secret'
        outside.write_bytes(b'untouched')
        (self.root / 'vault.salt').symlink_to(outside)
        with self.assertRaises(OSError):
            storage.initialize(self.root)
        self.assertEqual(outside.read_bytes(), b'untouched')

    @unittest.skipUnless(os.name == 'posix', 'POSIX ownership')
    def test_foreign_owner_rejected_without_chmod(self):
        self.root.mkdir()
        with mock.patch.object(storage.os, 'getuid', return_value=os.getuid() + 1):
            with self.assertRaises(OSError):
                storage.initialize(self.root)

    def test_atomic_write_failure_keeps_original(self):
        storage.write_private(self.root / 'known_hosts', b'old')
        with mock.patch.object(storage.os, 'replace', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                storage.write_private(self.root / 'known_hosts', b'new')
        self.assertEqual((self.root / 'known_hosts').read_bytes(), b'old')
        self.assertEqual([p.name for p in self.root.iterdir()], ['known_hosts'])

    def test_legacy_vault_and_database_roundtrip(self):
        self.root.mkdir()
        salt = bytes(range(16))
        # Exact 1.1.8 format, including its unchanged PBKDF2 parameters.
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        import base64
        key = base64.urlsafe_b64encode(PBKDF2HMAC(
            algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200000
        ).derive(b'test master'))
        fernet = Fernet(key)
        token = fernet.encrypt(b'old ssh password')
        (self.root / 'vault.salt').write_bytes(salt)
        verifier = fernet.encrypt(b'ssh-updater-keystore-v1')
        (self.root / 'vault.verify').write_bytes(verifier)
        with (mock.patch.object(crypto, 'DATA_DIR', self.root),
              mock.patch.object(crypto, '_SALT_PATH', self.root / 'vault.salt'),
              mock.patch.object(crypto, '_VERIFIER_PATH', self.root / 'vault.verify'),
              mock.patch.object(crypto, '_FERNET', None),
              mock.patch.object(db, 'DB_PATH', self.root / 'app.db')):
            crypto.set_master_password('test master')
            db.init_db()
            hid = db.add_or_update_host(proxmox_uid=None, name='legacy', primary_ip='server')
            with closing(db._connect()) as con, con:
                con.execute('UPDATE hosts SET password_enc=? WHERE id=?', (token, hid))
            db.init_db()
            with self.assertRaises(credentials.LegacyCredentialRequired):
                db.get_host_password(hid)
            crypto.set_master_password('test master')
            self.assertEqual(db.get_host(hid)['password_enc'], token)
            self.assertEqual((self.root / 'vault.salt').read_bytes(), salt)
            self.assertEqual((self.root / 'vault.verify').read_bytes(), verifier)
            with self.assertRaises(crypto.WrongPassword):
                crypto.set_master_password('wrong')
            # The transaction context alone would leave this handle open.
            import sqlite3
            with self.assertRaises(sqlite3.ProgrammingError):
                con.execute('SELECT 1')
            moved = (self.root / 'app.db').rename(self.root / 'renamed.db')
            moved.unlink()

    @unittest.skipUnless(os.name == 'posix', 'POSIX permissions')
    def test_sqlite_sidecars_are_private_even_with_permissive_umask(self):
        previous_umask = os.umask(0)
        try:
            with mock.patch.object(db, 'DB_PATH', self.root / 'app.db'):
                db.init_db()
                con = db._connect()
                try:
                    con.execute('PRAGMA journal_mode=WAL')
                    con.execute('INSERT INTO settings VALUES(?, ?)', ('test', '1'))
                    con.commit()
                    for path in self.root.glob('app.db*'):
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)
                finally:
                    con.close()
        finally:
            os.umask(previous_umask)

    def test_incomplete_vault_does_not_create_missing_counterpart(self):
        self.root.mkdir()
        path = self.root / 'vault.salt'
        path.write_bytes(bytes(range(16)))
        with (mock.patch.object(crypto, 'DATA_DIR', self.root),
              mock.patch.object(crypto, '_SALT_PATH', path),
              mock.patch.object(crypto, '_VERIFIER_PATH', self.root / 'vault.verify')):
            with self.assertRaisesRegex(OSError, 'unvollständig'):
                crypto.set_master_password('new')
        self.assertFalse((self.root / 'vault.verify').exists())
        self.assertEqual(path.read_bytes(), bytes(range(16)))

    @unittest.skipUnless(os.name == 'posix', 'POSIX permissions')
    def test_permission_correction_failure_is_not_ignored(self):
        self.root.mkdir()
        with mock.patch.object(storage.os, 'fchmod', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                storage.initialize(self.root)


class CredentialPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.object(db, 'DB_PATH', Path(self.temp.name) / 'app.db')
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(crypto, '_FERNET', Fernet(Fernet.generate_key()))
        patch.start(); self.addCleanup(patch.stop)
        db.init_db()
        self.values = dict(name='old', primary_ip='server', port=22, user='user', auth_method='password')
        self.hid = db.add_or_update_host(proxmox_uid=None, password_plain='secret', **self.values)

    def test_each_target_change_requires_decision_and_rolls_back(self):
        for field, value in [('primary_ip', 'other'), ('user', 'root'), ('port', 2222)]:
            for upsert in (False, True):
                with self.subTest(field=field, upsert=upsert):
                    values = dict(self.values, **{field: value})
                    with self.assertRaises(db.CredentialChangeRequired):
                        if upsert:
                            db.add_or_update_host(proxmox_uid=None, **values)
                        else:
                            db.update_host(self.hid, **values)
                    self.assertEqual(db.get_host(self.hid)[field], self.values[field])
                    self.assertEqual(db.get_host_password(self.hid), 'secret')

    def test_rename_retains_password(self):
        db.update_host(self.hid, **dict(self.values, name='renamed'))
        self.assertEqual(db.get_host_password(self.hid), 'secret')

    def test_key_switch_requires_explicit_choice_then_can_keep_or_delete(self):
        values = dict(self.values, auth_method='key')
        with self.assertRaises(db.CredentialChangeRequired):
            db.update_host(self.hid, **values)
        db.update_host(self.hid, keep_password=True, **values)
        self.assertEqual(db.get_host_password(self.hid), 'secret')
        with self.assertRaises(db.CredentialChangeRequired):
            db.update_host(self.hid, **dict(values, port=23))
        db.update_host(self.hid, delete_password=True, **dict(values, port=23))
        self.assertIsNone(db.get_host_password(self.hid))

    def test_new_target_with_reentered_password(self):
        db.update_host(self.hid, password_plain='new', **dict(self.values, primary_ip='other'))
        self.assertEqual(db.get_host_password(self.hid), 'new')


class SSHSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Exercise AsyncSSH's real default-file lookup without touching user files.
        self.external_ssh = self.root / 'external-home' / '.ssh'
        self.external_ssh.mkdir(parents=True)
        expanduser = os.path.expanduser
        patch = mock.patch('os.path.expanduser', side_effect=lambda path:
                           str(self.external_ssh.parent) + os.fspath(path)[1:]
                           if os.fspath(path) == '~' or os.fspath(path).startswith('~/') else expanduser(path))
        patch.start(); self.addCleanup(patch.stop)
        self.assertEqual(Path('~', '.ssh', 'known_hosts').expanduser(), self.external_ssh / 'known_hosts')
        for name, value in [('DATA_DIR', self.root), ('KNOWN_HOSTS', self.root / 'known_hosts')]:
            patch = mock.patch.object(settings, name, value)
            patch.start(); self.addCleanup(patch.stop)
        self.config = self.root / 'config'
        self.config.write_text('', encoding="utf-8")
        original = ssh_connection.options_for
        patch = mock.patch.object(ssh_connection, 'options_for',
                                  side_effect=lambda host, **kw: original(host, config=[self.config], **kw))
        patch.start(); self.addCleanup(patch.stop)
        # These tests isolate transport policy. Real DB snapshot/codec integration
        # is exercised in test_credentials_v2, including refusal before any socket.
        patch = mock.patch.object(db, 'get_connection_context', side_effect=lambda host: host)
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(crypto, 'decrypt_host_password', return_value='secret')
        patch.start(); self.addCleanup(patch.stop)
        self.key = asyncssh.generate_private_key('ssh-ed25519')
        self.auth = []
        self.commands = []
        self.servers = []
        self.host = dict(id=1, password_enc=b'synthetic-transport-fixture', name='test', primary_ip='127.0.0.1', user='user', auth_method='password')
        self.host['port'] = await self.start_server(self.key)

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()

    async def start_server(self, key, port=0, public_key=None, kbdint=False,
                           host_certificate=None, client_ca=None, process_factory=None):
        events = self.auth
        commands = self.commands
        class Server(asyncssh.SSHServer):
            def begin_auth(self, username):
                events.append(('begin', username))
                return True
            def password_auth_supported(self):
                return not kbdint
            def kbdint_auth_supported(self):
                return kbdint
            def get_kbdint_challenge(self, username, lang, submethods):
                return ('', '', 'en', [('Password:', False)])
            def validate_kbdint_response(self, username, responses):
                events.append(('keyboard-interactive', responses))
                return responses == ['secret']
            def validate_password(self, username, password):
                events.append(('password', password))
                return password == 'secret'
            def public_key_auth_supported(self):
                return public_key is not None or client_ca is not None
            def validate_ca_key(self, username, key):
                events.append(('client-ca', key))
                return key == client_ca
            def validate_public_key(self, username, key):
                return key == public_key
            def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
                return True
        def process(proc):
            self.assertIsNone(proc.term_type)
            commands.append(proc.command)
            proc.stdout.write('ID=debian\n')
            proc.exit(0)
        server = await asyncssh.create_server(Server, '127.0.0.1', port,
                    server_host_keys=[key], server_host_certs=[host_certificate] if host_certificate else [],
                    process_factory=process_factory or process)
        self.servers.append(server)
        return server.get_port()

    async def trust(self, host=None):
        observation = await ssh_connection.inspect_host(host or self.host)
        host_keys.confirm(observation)
        return observation

    async def test_real_channel_output_budget_and_subsequent_command(self):
        from sshupdater.core import remote_process
        def process(proc):
            proc.stdout.write('x' * 200000 if proc.command == 'large' else 'next')
            proc.exit(0)
        host = dict(self.host, port=await self.start_server(self.key, process_factory=process))
        await self.trust(host)
        async with ssh_connection.connect_host(host) as conn:
            with mock.patch.object(remote_process, 'CAPTURE_LIMIT', 32000):
                with self.assertRaisesRegex(remote_process.RemoteWaitError, 'Ausgabelimit'):
                    await remote_process.capture(conn, 'large', 2)
            self.assertEqual(await remote_process.capture(conn, 'small', 2), (0, 'next', ''))

    async def test_real_channel_timeout_and_user_cancellation(self):
        from sshupdater.core import remote_process
        processes = []
        def process(proc):
            processes.append(proc)
        host = dict(self.host, port=await self.start_server(self.key, process_factory=process))
        await self.trust(host)
        async with ssh_connection.connect_host(host) as conn:
            with self.assertRaises(remote_process.RemoteTimeoutError):
                await remote_process.capture(conn, 'hang', .05)
            task = asyncio.create_task(remote_process.capture(conn, 'hang again', 2))
            while len(processes) < 2:
                await asyncio.sleep(.005)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(processes), 2)

    async def test_connect_timeout_on_server_without_ssh_banner(self):
        clients = []
        async def accept(reader, writer):
            clients.append(writer)
            await reader.read()
            writer.close()
        server = await asyncio.start_server(accept, '127.0.0.1', 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        host = dict(self.host, port=server.sockets[0].getsockname()[1])
        with mock.patch.object(ssh_connection, 'CONNECT_TIMEOUT', .05):
            with self.assertRaises(TimeoutError):
                async with ssh_connection.connect_host(host):
                    self.fail('No SSH handshake should complete')
        for writer in clients:
            writer.close()
            await writer.wait_closed()

    async def test_transport_rejects_oversized_header_before_payload_or_auth(self):
        """Real TCP attack: only eight header bytes, never allocate the payload."""
        import tracemalloc
        for length in (256 * 1024 + 1, 0xfffffff0):
            writers, handlers = [], []
            async def attack(reader, writer):
                writers.append(writer)
                handlers.append(asyncio.current_task())
                try:
                    writer.write(b'SSH-2.0-oversized-test\r\n')
                    await writer.drain()
                    await reader.readline()
                    # Fragmented header must be rejected as soon as complete.
                    header = length.to_bytes(4, 'big') + b'\x04xxx'
                    writer.write(header[:3])
                    await writer.drain()
                    await asyncio.sleep(.005)
                    writer.write(header[3:])
                    await writer.drain()
                    while await reader.read(8192):
                        pass
                finally:
                    writer.close()
            server = await asyncio.start_server(attack, '127.0.0.1', 0)
            host = dict(self.host, port=server.sockets[0].getsockname()[1])
            tracemalloc.start()
            try:
                with self.assertRaisesRegex(asyncssh.ProtocolError, 'Max packet size'):
                    async with asyncio.timeout(1):
                        async with ssh_connection.connect_host(host):
                            self.fail('Oversized header accepted')
                _, peak = tracemalloc.get_traced_memory()
                self.assertLess(peak, 2 * 1024 * 1024)
                self.assertEqual(self.auth, [])
            finally:
                tracemalloc.stop()
                server.close()
                await server.wait_closed()
                for writer in writers:
                    writer.close()
                    await writer.wait_closed()
                await asyncio.gather(*handlers, return_exceptions=True)

    async def test_real_eof_without_channel_close_hits_idle_deadline(self):
        from types import SimpleNamespace
        from sshupdater.core import remote_process
        send_eof = asyncio.Event()
        server_processes = []
        async def process(proc):
            server_processes.append(proc)
            await send_eof.wait()
            proc.stdout.write('complete output')
            proc.stdout.write_eof()
            # Deliberately omit channel close and exit status.
        host = dict(self.host, port=await self.start_server(self.key, process_factory=process))
        await self.trust(host)
        async with ssh_connection.connect_host(host) as conn:
            # Establish and confirm the real SSH session before testing the
            # short channel-close deadline. Network setup has its own budget.
            async with asyncio.timeout(5):
                proc = await conn.create_process('test', encoding=None, request_pty=False)
                send_eof.set()
                self.assertEqual(await proc.stdout.read(), b'complete output')
                self.assertEqual(await proc.stderr.read(), b'')
            self.assertEqual(len(server_processes), 1)
            self.assertIsNone(proc.exit_status)
            self.assertFalse(proc.channel.is_closing())
            prepared = SimpleNamespace(create_process=mock.AsyncMock(return_value=proc))
            with self.assertRaisesRegex(remote_process.RemoteTimeoutError, 'Kanalschluss'):
                async for _ in remote_process.output(prepared, 'test', timeout=5,
                                                     limit=1024, idle_timeout=.1):
                    pass

    async def test_real_channel_open_without_ack_hits_idle_deadline(self):
        from sshupdater.core import remote_process
        pending = []
        entered = asyncio.Event()
        class Server(asyncssh.SSHServer):
            def begin_auth(self, username):
                return False
            async def session_requested(self):
                pending.append(asyncio.current_task())
                entered.set()
                await asyncio.Event().wait()
        server = await asyncssh.create_server(Server, '127.0.0.1', 0,
                                              server_host_keys=[self.key])
        self.servers.append(server)
        host = dict(self.host, port=server.get_port())
        await self.trust(host)
        try:
            async with ssh_connection.connect_host(host) as conn:
                with self.assertRaisesRegex(remote_process.RemoteTimeoutError, 'Prozessanforderung'):
                    async for _ in remote_process.output(conn, 'test', timeout=2,
                                                         limit=1024, idle_timeout=.03):
                        pass
                self.assertTrue(entered.is_set())
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def external_trust(self, key, port=None):
        (self.external_ssh / 'known_hosts').write_bytes(
            f"[127.0.0.1]:{port or self.host['port']} ".encode() + key.export_public_key())

    async def assert_blocked_without_auth(self, host=None, changed=False):
        before = settings.KNOWN_HOSTS.read_bytes() if settings.KNOWN_HOSTS.exists() else None
        with self.assertRaises(host_keys.ReviewRequired) as ctx:
            async with ssh_connection.connect_host(host or self.host):
                self.fail('Untrusted server accepted')
        self.assertEqual(ctx.exception.observation.changed, changed)
        self.assertEqual(self.auth, [])
        self.assertEqual(self.commands, [])
        after = settings.KNOWN_HOSTS.read_bytes() if settings.KNOWN_HOSTS.exists() else None
        self.assertEqual(after, before)

    async def test_external_known_hosts_cannot_trust_unknown_application_host(self):
        self.external_trust(self.key)
        await self.assert_blocked_without_auth()
        observation = await ssh_connection.inspect_host(self.host)
        self.assertEqual(observation.key, self.key.convert_to_public())
        self.assertEqual(self.auth, [])

    async def test_external_known_hosts_cannot_override_application_pin(self):
        await self.trust()
        wrong = asyncssh.generate_private_key('ssh-ed25519')
        settings.KNOWN_HOSTS.write_bytes(
            f"[127.0.0.1]:{self.host['port']} ".encode() + wrong.export_public_key())
        self.external_trust(self.key)
        await self.assert_blocked_without_auth(changed=True)

    async def test_rotated_key_still_blocked_when_externally_trusted(self):
        await self.trust()
        self.servers[0].close()
        await self.servers[0].wait_closed()
        replacement = asyncssh.generate_private_key('ssh-ed25519')
        await self.start_server(replacement, self.host['port'])
        self.external_trust(replacement)
        await self.assert_blocked_without_auth(changed=True)

    async def test_proxyjump_external_trust_cannot_override_application_pin(self):
        jump_key = asyncssh.generate_private_key('ssh-ed25519')
        port = await self.start_server(jump_key)
        self.config.write_text('Host target\n HostName 127.0.0.1\n ProxyJump jump\n'
                               f'Host jump\n HostName 127.0.0.1\n Port {port}\n', encoding="utf-8")
        wrong = asyncssh.generate_private_key('ssh-ed25519')
        settings.KNOWN_HOSTS.write_bytes(f'[127.0.0.1]:{port} '.encode() + wrong.export_public_key())
        self.external_trust(jump_key, port)
        await self.assert_blocked_without_auth(dict(self.host, primary_ip='target'), changed=True)

    async def test_host_certificate_and_raw_key_with_client_certificate(self):
        ca = asyncssh.generate_private_key('ssh-ed25519')
        host_cert = ca.generate_host_certificate(self.key, 'host', principals=['127.0.0.1'])
        client = asyncssh.generate_private_key('ssh-ed25519')
        identity = self.root / 'identity'
        certificate = self.root / 'client-cert.pub'
        client.write_private_key(identity)
        ca.generate_user_certificate(client, 'client', principals=['user']).write_certificate(certificate)
        self.config.write_text(f'Host *\n CertificateFile {ssh_config_path(certificate)}\n'
                               ' HostKeyAlgorithms ssh-ed25519-cert-v01@openssh.com,ssh-ed25519\n', encoding="utf-8")
        port = await self.start_server(self.key, host_certificate=host_cert, client_ca=ca.convert_to_public())
        host = dict(self.host, port=port, auth_method='key', key_path=str(identity))
        observation = await self.trust(host)
        self.assertEqual(observation.key, self.key.convert_to_public())
        self.assertEqual(self.auth, [])
        async with ssh_connection.connect_host(host) as conn:
            await conn.run('certificate login', check=True)
        self.assertIn(('client-ca', ca.convert_to_public()), self.auth)
        self.assertNotIn(('password', 'secret'), self.auth)

    async def test_certificate_only_algorithm_config_fails_before_auth(self):
        self.config.write_text('Host *\n HostKeyAlgorithms ssh-ed25519-cert-v01@openssh.com\n', encoding="utf-8")
        with self.assertRaisesRegex(OSError, 'HostKeyAlgorithms'):
            async with ssh_connection.connect_host(self.host):
                self.fail('Unsupported host certificates accepted')
        self.assertEqual(self.auth, [])

    async def test_unknown_rejected_before_auth_and_inspection_does_not_login(self):
        with self.assertRaises(host_keys.ReviewRequired):
            async with ssh_connection.connect_host(self.host):
                self.fail('must reject')
        observation = await ssh_connection.inspect_host(self.host)
        self.assertEqual(observation.key, self.key.convert_to_public())
        self.assertEqual(self.auth, [])
        self.assertFalse(settings.KNOWN_HOSTS.exists())

    async def test_known_key_connects_and_changed_key_blocks_before_auth(self):
        await self.trust()
        async with ssh_connection.connect_host(self.host) as conn:
            await conn.run('test', check=True)
        self.assertIn(('password', 'secret'), self.auth)
        self.auth.clear()
        self.servers[0].close(); await self.servers[0].wait_closed()
        await self.start_server(asyncssh.generate_private_key('ssh-ed25519'), self.host['port'])
        previous = settings.KNOWN_HOSTS.read_bytes()
        with self.assertRaises(host_keys.ReviewRequired) as ctx:
            async with ssh_connection.connect_host(self.host):
                self.fail('must reject')
        self.assertTrue(ctx.exception.observation.changed)
        self.assertEqual(self.auth, [])
        self.assertEqual(settings.KNOWN_HOSTS.read_bytes(), previous)
        replacement = await ssh_connection.inspect_host(self.host)
        self.assertTrue(replacement.changed)
        host_keys.confirm(replacement)
        async with ssh_connection.connect_host(self.host):
            pass

    async def test_config_alias_identity_and_forwarding_override(self):
        client_key = asyncssh.generate_private_key('ssh-ed25519')
        path = self.root / 'identity'
        client_key.write_private_key(path)
        port = await self.start_server(self.key, public_key=client_key.convert_to_public())
        self.config.write_text(f'Host alias\n HostName 127.0.0.1\n IdentityFile {ssh_config_path(path)}\n'
                               ' ForwardAgent yes\n ForwardX11 yes\n ForwardX11Trusted yes\n RequestTTY force\n'
                               ' User wrong\n Port 1\n HostKeyAlias pinned-alias\n', encoding="utf-8")
        host = dict(self.host, primary_ip='alias', port=port, auth_method='key')
        observation = await self.trust(host)
        self.assertEqual(observation.host, 'pinned-alias')
        options = ssh_connection.options_for(host)
        self.assertEqual(options.host, '127.0.0.1')
        self.assertEqual(options.username, 'user')
        self.assertEqual(options.port, port)
        self.assertFalse(options.agent_forward_path)
        self.assertFalse(options.x11_forwarding)
        async with ssh_connection.connect_host(host) as conn:
            self.assertFalse(conn._agent_forward_path)
            self.assertFalse(conn._options.x11_forwarding)
            from sshupdater.core.remote_process import capture
            self.assertEqual((await capture(conn, 'no forwarding', 2))[0], 0)
        self.assertNotIn(('password', 'secret'), self.auth)

    async def test_password_mode_does_not_use_config_keys_or_agent(self):
        self.config.write_text('Host *\n ForwardAgent yes\n IdentityFile /nonexistent\n'
                               ' IdentityAgent /nonexistent\n GSSAPIAuthentication yes\n', encoding="utf-8")
        options = ssh_connection.options_for(self.host)
        self.assertIsNone(options.client_keys)
        self.assertFalse(options.agent_path)
        self.assertFalse(options.agent_forward_path)
        self.assertFalse(options.public_key_auth)
        self.assertFalse(options.gss_auth)
        self.assertFalse(options.host_based_auth)
        await self.trust()
        async with ssh_connection.connect_host(self.host):
            pass

    async def test_proxyjump_uses_same_trust_policy(self):
        client_key = asyncssh.generate_private_key('ssh-ed25519')
        path = self.root / 'identity'; client_key.write_private_key(path)
        jump_key = asyncssh.generate_private_key('ssh-ed25519')
        jump_port = await self.start_server(jump_key, public_key=client_key.convert_to_public())
        self.config.write_text(f'Host target\n HostName 127.0.0.1\n ProxyJump jump\n'
                               f'Host jump\n HostName 127.0.0.1\n Port {jump_port}\n'
                               f' IdentityFile {ssh_config_path(path)}\n'
                               'Host *\n ForwardAgent yes\n ForwardX11 yes\n ForwardX11Trusted yes\n RequestTTY force\n', encoding="utf-8")
        host = dict(self.host, primary_ip='target')
        jump = await self.trust(host)
        self.assertEqual(jump.port, jump_port)
        self.assertEqual(self.auth, [])
        target = await self.trust(host)
        self.assertEqual(target.port, self.host['port'])
        original_connect = asyncssh.connect
        connections = []
        async def connect(*args, **kwargs):
            conn = await original_connect(*args, **kwargs)
            connections.append(conn)
            return conn
        with mock.patch.object(asyncssh, 'connect', connect):
            async with ssh_connection.connect_host(host) as conn:
                from sshupdater.core.remote_process import capture
                self.assertEqual((await capture(conn, 'through jump', 2))[0], 0)
                self.assertEqual(len(connections), 2)
                for connection in connections:
                    self.assertFalse(connection._options.request_pty)
                    self.assertFalse(connection._options.x11_forwarding)
                    self.assertFalse(connection._agent_forward_path)
        self.assertIn('through jump', self.commands)

    async def test_all_actions_reject_unknown_server(self):
        from sshupdater.core import ssh_client
        for operation in (ssh_client.check_updates_for_host, ssh_client.simulate_upgrade_for_host,
                          ssh_client.simulate_autoremove_for_host, ssh_client.reboot_host):
            result = await operation(self.host)
            self.assertEqual(result['status'], 'error')
            self.assertIn('Serveridentität', result['note'])
        for operation in (ssh_client.upgrade_host_stream, ssh_client.autoremove_host_stream):
            results = [x async for x in operation(self.host)]
            self.assertEqual(results[-1]['result']['status'], 'error')
            self.assertIn('Serveridentität', results[-1]['result']['note'])
        self.assertEqual(self.auth, [])
        self.assertEqual(self.commands, [])

    async def test_stale_confirmation_and_malformed_store_rejected(self):
        first = await ssh_connection.inspect_host(self.host)
        host_keys.confirm(first)
        with self.assertRaises(OSError):
            host_keys.confirm(first)
        settings.KNOWN_HOSTS.write_text('broken\n', encoding="utf-8")
        with self.assertRaises(OSError):
            async with ssh_connection.connect_host(self.host):
                self.fail('must reject')

    async def test_keyboard_interactive_password_compatibility(self):
        port = await self.start_server(self.key, kbdint=True)
        host = dict(self.host, port=port)
        await self.trust(host)
        async with ssh_connection.connect_host(host):
            pass
        self.assertIn(('keyboard-interactive', ['secret']), self.auth)

    async def test_explicit_key_excludes_other_config_identities(self):
        key = asyncssh.generate_private_key('ssh-ed25519')
        path = self.root / 'chosen'; key.write_private_key(path)
        self.config.write_text('Host *\n IdentityFile /nonexistent\n'
                               ' IdentityAgent /nonexistent\n ForwardAgent yes\n', encoding="utf-8")
        host = dict(self.host, auth_method='key', key_path=str(path))
        options = ssh_connection.options_for(host)
        self.assertFalse(options.agent_path)
        self.assertFalse(options.password_auth)
        self.assertFalse(options.kbdint_auth)
        self.assertEqual(len(options.client_keys), 1)
        self.assertEqual(options.client_keys[0].get_algorithm(), key.get_algorithm())

    async def test_proxycommand_keeps_transport_and_verifies_target(self):
        proxy = self.root / 'proxy.py'
        proxy.write_text("""import socket, sys, threading
sock = socket.create_connection((sys.argv[1], int(sys.argv[2])))
def send():
    while data := sys.stdin.buffer.read1(65536):
        sock.sendall(data)
    sock.shutdown(socket.SHUT_WR)
threading.Thread(target=send, daemon=True).start()
while True:
    data = sock.recv(65536)
    if not data:
        sys.exit(0)
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
""", encoding="utf-8")
        self.config.write_text(f'Host proxy-target\n HostName 127.0.0.1\n'
                               f' ProxyCommand {ssh_config_path(Path(sys.executable))} {ssh_config_path(proxy)} %h %p\n'
                               ' ConnectTimeout 5\n ServerAliveInterval 7\n', encoding="utf-8")
        host = dict(self.host, primary_ip='proxy-target')
        options = ssh_connection.options_for(host)
        self.assertEqual(options.keepalive_interval, 7)
        loop = asyncio.get_running_loop()
        subprocess_exec = loop.subprocess_exec
        children = []
        async def record_child(protocol_factory, *args, **kwargs):
            exited = asyncio.Event()
            def factory():
                protocol = protocol_factory()
                original_exited = protocol.process_exited
                def process_exited():
                    original_exited()
                    exited.set()
                protocol.process_exited = process_exited
                return protocol
            transport, protocol = await subprocess_exec(factory, *args, **kwargs)
            children.append((transport, exited))
            return transport, protocol
        with mock.patch.object(loop, 'subprocess_exec', side_effect=record_child):
            await self.trust(host)
            async with ssh_connection.connect_host(host) as conn:
                await conn.run('via proxy', check=True)
        self.assertIn('via proxy', self.commands)
        self.assertEqual(len(children), 2)  # Inspection and authenticated session.
        # Closing a transport requests termination; the OS exit notification
        # is asynchronous. Await that notification, not a timing-based sleep.
        async with asyncio.timeout(5):
            await asyncio.gather(*(exited.wait() for _, exited in children))
        for child, _ in children:
            self.assertTrue(child.is_closing())
            self.assertIsNotNone(child.get_returncode(), 'ProxyCommand child still running')

    async def test_all_actions_also_block_changed_server(self):
        await self.trust()
        self.servers[0].close()
        await self.servers[0].wait_closed()
        await self.start_server(asyncssh.generate_private_key('ssh-ed25519'), self.host['port'])
        await self.test_all_actions_reject_unknown_server()

    async def test_certificatefile_matching_explicit_identity_is_preserved(self):
        key = asyncssh.generate_private_key('ssh-ed25519')
        ca = asyncssh.generate_private_key('ssh-ed25519')
        identity = self.root / 'chosen'
        certificate = self.root / 'separate-cert.pub'
        key.write_private_key(identity)
        ca.generate_user_certificate(key, 'test-user', principals=['user']).write_certificate(certificate)
        self.config.write_text(f'Host *\n IdentityFile /nonexistent\n CertificateFile {ssh_config_path(certificate)}\n', encoding="utf-8")
        options = ssh_connection.options_for(dict(self.host, auth_method='key', key_path=str(identity)))
        self.assertEqual(len(options.client_keys), 2)
        self.assertTrue(any('cert-v01' in pair.get_algorithm() for pair in options.client_keys))
        self.assertFalse(options.agent_path)


class SSHConfigPathTests(unittest.TestCase):
    def test_windows_and_posix_paths_survive_real_config_parser(self):
        import io
        from pathlib import PureWindowsPath, PurePosixPath
        from asyncssh.config import SSHClientConfig
        for path in (PureWindowsPath(r'C:\Users\Test User\keys\id_ed25519'),
                     PureWindowsPath(r'\\server\share name\client-cert.pub'),
                     PurePosixPath('/tmp/test user/key"withquote')):
            text = f'Host *\n IdentityFile {ssh_config_path(path)}\n CertificateFile {ssh_config_path(path)}\n'
            with mock.patch('builtins.open', side_effect=lambda *a, **kw: io.StringIO(text)):
                config = SSHClientConfig.load(None, ['synthetic'], False, False, False,
                                              'local', 'user', 'host', 22)
            self.assertEqual(config.get('IdentityFile'), [path.as_posix()])
            self.assertEqual(config.get('CertificateFile'), [path.as_posix()])
