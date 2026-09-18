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
from sshupdater.core import crypto, db, host_keys, settings, ssh_connection, storage


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
            with db._connect() as con:
                con.execute('UPDATE hosts SET password_enc=? WHERE id=?', (token, hid))
            db.init_db()
            self.assertEqual(db.get_host_password(hid), 'old ssh password')
            self.assertEqual(db.get_host(hid)['password_enc'], token)
            self.assertEqual((self.root / 'vault.salt').read_bytes(), salt)
            self.assertEqual((self.root / 'vault.verify').read_bytes(), verifier)
            with self.assertRaises(crypto.WrongPassword):
                crypto.set_master_password('wrong')

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
        self.config.write_text('')
        original = ssh_connection.options_for
        patch = mock.patch.object(ssh_connection, 'options_for',
                                  side_effect=lambda host, **kw: original(host, config=[self.config], **kw))
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(db, 'get_host_password', return_value='secret')
        patch.start(); self.addCleanup(patch.stop)
        self.key = asyncssh.generate_private_key('ssh-ed25519')
        self.auth = []
        self.commands = []
        self.servers = []
        self.host = dict(id=1, name='test', primary_ip='127.0.0.1', user='user', auth_method='password')
        self.host['port'] = await self.start_server(self.key)

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()

    async def start_server(self, key, port=0, public_key=None, kbdint=False,
                           host_certificate=None, client_ca=None):
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
            commands.append(proc.command)
            proc.stdout.write('ID=debian\n')
            proc.exit(0)
        server = await asyncssh.create_server(Server, '127.0.0.1', port,
                    server_host_keys=[key], server_host_certs=[host_certificate] if host_certificate else [],
                    process_factory=process)
        self.servers.append(server)
        return server.get_port()

    async def trust(self, host=None):
        observation = await ssh_connection.inspect_host(host or self.host)
        host_keys.confirm(observation)
        return observation

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
                               f'Host jump\n HostName 127.0.0.1\n Port {port}\n')
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
        self.config.write_text(f'Host *\n CertificateFile {certificate}\n'
                               ' HostKeyAlgorithms ssh-ed25519-cert-v01@openssh.com,ssh-ed25519\n')
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
        self.config.write_text('Host *\n HostKeyAlgorithms ssh-ed25519-cert-v01@openssh.com\n')
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
        self.config.write_text(f'Host alias\n HostName 127.0.0.1\n IdentityFile {path}\n'
                               ' ForwardAgent yes\n User wrong\n Port 1\n HostKeyAlias pinned-alias\n')
        host = dict(self.host, primary_ip='alias', port=port, auth_method='key')
        observation = await self.trust(host)
        self.assertEqual(observation.host, 'pinned-alias')
        options = ssh_connection.options_for(host)
        self.assertEqual(options.host, '127.0.0.1')
        self.assertEqual(options.username, 'user')
        self.assertEqual(options.port, port)
        self.assertFalse(options.agent_forward_path)
        async with ssh_connection.connect_host(host) as conn:
            self.assertFalse(conn._agent_forward_path)
        self.assertNotIn(('password', 'secret'), self.auth)

    async def test_password_mode_does_not_use_config_keys_or_agent(self):
        self.config.write_text('Host *\n ForwardAgent yes\n IdentityFile /nonexistent\n'
                               ' IdentityAgent /nonexistent\n GSSAPIAuthentication yes\n')
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
                               f' IdentityFile {path}\n ForwardAgent yes\n')
        host = dict(self.host, primary_ip='target')
        jump = await self.trust(host)
        self.assertEqual(jump.port, jump_port)
        self.assertEqual(self.auth, [])
        target = await self.trust(host)
        self.assertEqual(target.port, self.host['port'])
        async with ssh_connection.connect_host(host) as conn:
            await conn.run('through jump', check=True)
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
        settings.KNOWN_HOSTS.write_text('broken\n')
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
                               ' IdentityAgent /nonexistent\n ForwardAgent yes\n')
        host = dict(self.host, auth_method='key', key_path=str(path))
        options = ssh_connection.options_for(host)
        self.assertFalse(options.agent_path)
        self.assertFalse(options.password_auth)
        self.assertFalse(options.kbdint_auth)
        self.assertEqual(len(options.client_keys), 1)
        self.assertEqual(options.client_keys[0].get_algorithm(), key.get_algorithm())

    async def test_proxycommand_keeps_transport_and_verifies_target(self):
        proxy = self.root / 'proxy.py'
        proxy.write_text("""import socket, sys, select, os
sock = socket.create_connection((sys.argv[1], int(sys.argv[2])))
while True:
    readable, _, _ = select.select([sock, sys.stdin.buffer], [], [])
    for source in readable:
        data = sock.recv(65536) if source is sock else os.read(0, 65536)
        if not data:
            sys.exit(0)
        if source is sock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        else:
            sock.sendall(data)
""")
        self.config.write_text(f'Host proxy-target\n HostName 127.0.0.1\n'
                               f' ProxyCommand {sys.executable} {proxy} %h %p\n'
                               ' ConnectTimeout 5\n ServerAliveInterval 7\n')
        host = dict(self.host, primary_ip='proxy-target')
        options = ssh_connection.options_for(host)
        self.assertEqual(options.keepalive_interval, 7)
        await self.trust(host)
        async with ssh_connection.connect_host(host) as conn:
            await conn.run('via proxy', check=True)
        self.assertIn('via proxy', self.commands)

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
        self.config.write_text(f'Host *\n IdentityFile /nonexistent\n CertificateFile {certificate}\n')
        options = ssh_connection.options_for(dict(self.host, auth_method='key', key_path=str(identity)))
        self.assertEqual(len(options.client_keys), 2)
        self.assertTrue(any('cert-v01' in pair.get_algorithm() for pair in options.client_keys))
        self.assertFalse(options.agent_path)
