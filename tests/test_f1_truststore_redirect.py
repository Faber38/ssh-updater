"""Regressionstest fuer F1: known_hosts-Umleitung wird durch das Integritaetssiegel geblockt.

Ablegen unter tests/ und mit der uebrigen Suite laufen lassen.
"""
import asyncio
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import asyncssh

from sshupdater.core import settings, storage, crypto, db, host_keys, ssh_connection, ssh_client


class TruststoreRedirectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.root = root
        for module, name, value in (
            (settings, 'DATA_DIR', root),
            (settings, 'KNOWN_HOSTS', root / 'known_hosts'),
            (settings, 'KNOWN_HOSTS_MAC', root / 'known_hosts.mac'),
            (crypto, 'DATA_DIR', root),
            (crypto, '_SALT_PATH', root / 'vault.salt'),
            (crypto, '_VERIFIER_PATH', root / 'vault.verify'),
            (crypto, '_FERNET', None),
            (crypto, '_MAC_KEY', None),
            (db, 'DB_PATH', root / 'app.db'),
        ):
            patch = mock.patch.object(module, name, value)
            patch.start(); self.addCleanup(patch.stop)
        storage.initialize(root)
        crypto.set_master_password('correct horse battery staple')
        host_keys.initialize_fresh()
        db.init_db()

        self.key_real = asyncssh.generate_private_key('ssh-ed25519')
        self.key_attacker = asyncssh.generate_private_key('ssh-ed25519')
        self.captured = {}
        self.servers = []
        self.port = await self._server('127.0.0.1', 0, self.key_real, 'real')
        await self._server('127.0.0.2', self.port, self.key_attacker, 'attacker')

        # Aufloesung von prod.lan steuerbar machen (simuliert /etc/hosts).
        self.attack = False
        loop = asyncio.get_event_loop()
        real_gai = loop.getaddrinfo

        async def gai(host, port, *a, **k):
            if host == 'prod.lan':
                target = '127.0.0.2' if self.attack else '127.0.0.1'
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (target, port))]
            return await real_gai(host, port, *a, **k)

        patch = mock.patch.object(loop, 'getaddrinfo', side_effect=gai)
        patch.start(); self.addCleanup(patch.stop)

        self.hid = db.add_or_update_host(
            proxmox_uid=None, name='target', primary_ip='prod.lan', port=self.port,
            user='root', auth_method='password', password_plain='PROD-PASSWORT')

    async def asyncTearDown(self):
        for s in self.servers:
            s.close()
            await s.wait_closed()

    async def _server(self, addr, port, key, label):
        tests = self

        class Server(asyncssh.SSHServer):
            def begin_auth(self, username):
                return True

            def password_auth_supported(self):
                return True

            def validate_password(self, username, password):
                tests.captured[label] = (username, password)
                return True

        def process(proc):
            proc.stdout.write('ID=debian\n')
            proc.exit(0)

        server = await asyncssh.create_server(
            Server, addr, port, server_host_keys=[key], process_factory=process)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def _trust_real(self):
        observation = await ssh_connection.inspect_host(db.get_host(self.hid))
        host_keys.confirm(observation)

    async def test_redirect_after_pin_tamper_is_blocked(self):
        await self._trust_real()
        self.assertTrue(settings.KNOWN_HOSTS_MAC.exists())

        # Normalbetrieb: Passwort geht an den echten Server.
        self.captured.clear()
        await ssh_client.check_updates_for_host(db.get_host(self.hid))
        self.assertEqual(self.captured.get('real'), ('root', 'PROD-PASSWORT'))
        self.assertNotIn('attacker', self.captured)

        # Angriff: Pin faelschen und Aufloesung umbiegen. app.db bleibt unangetastet.
        self.captured.clear()
        self.attack = True
        storage.write_private(
            settings.KNOWN_HOSTS,
            f"[prod.lan]:{self.port} ".encode()
            + self.key_attacker.export_public_key('openssh'))

        result = await ssh_client.check_updates_for_host(db.get_host(self.hid))

        self.assertEqual(result.get('status'), 'error')
        self.assertNotIn('attacker', self.captured)
        self.assertNotIn('real', self.captured)

    async def test_tampered_store_raises_integrity_error(self):
        await self._trust_real()
        storage.write_private(
            settings.KNOWN_HOSTS,
            f"[prod.lan]:{self.port} ".encode()
            + self.key_attacker.export_public_key('openssh'))
        with self.assertRaises(host_keys.TruststoreIntegrityError):
            host_keys.load()

    async def test_missing_or_malformed_mac_never_auto_signs_existing_store(self):
        await self._trust_real()
        original = settings.KNOWN_HOSTS.read_bytes()
        for tag in (None, b'truncated'):
            with self.subTest(tag=tag):
                if tag is None:
                    settings.KNOWN_HOSTS_MAC.unlink(missing_ok=True)
                else:
                    storage.write_private(settings.KNOWN_HOSTS_MAC, tag)
                with self.assertRaises(host_keys.TruststoreIntegrityError):
                    host_keys.load()
                self.assertEqual(settings.KNOWN_HOSTS.read_bytes(), original)
                if tag is None:
                    self.assertFalse(settings.KNOWN_HOSTS_MAC.exists())

    async def test_store_and_mac_deletion_requires_explicit_reinitialization(self):
        await self._trust_real()
        settings.KNOWN_HOSTS.unlink()
        with self.assertRaises(host_keys.TruststoreIntegrityError):
            host_keys.load()
        settings.KNOWN_HOSTS_MAC.unlink()
        with self.assertRaises(host_keys.TruststoreInitializationRequired):
            host_keys.load()
        host_keys.reset_for_reconfirmation()
        self.assertEqual(host_keys.load(), {})

    async def test_legacy_store_is_quarantined_before_reconfirmation(self):
        await self._trust_real()
        original = settings.KNOWN_HOSTS.read_bytes()
        settings.KNOWN_HOSTS_MAC.unlink()
        with self.assertRaises(host_keys.TruststoreMigrationRequired):
            host_keys.load()
        host_keys.reset_for_reconfirmation()
        self.assertEqual(settings.KNOWN_HOSTS.with_name('known_hosts.untrusted').read_bytes(),
                         original)
        self.assertEqual(host_keys.load(), {})

    async def test_validator_rechecks_integrity_before_releasing_password(self):
        await self._trust_real()
        host = db.get_connection_context(db.get_host(self.hid))
        validator = host_keys.Validator('prod.lan', self.port, 'prod.lan',
                                        credential_host=host)
        with mock.patch.object(crypto, 'decrypt_host_password',
                               return_value='PROD-PASSWORT') as decrypt:
            self.assertIsNone(validator.password_auth_requested())
            decrypt.assert_not_called()
            settings.KNOWN_HOSTS_MAC.unlink()
            with self.assertRaises(host_keys.TruststoreMigrationRequired):
                validator.validate_host_public_key(
                    'prod.lan', '127.0.0.1', self.port, self.key_real.convert_to_public())
            decrypt.assert_not_called()

    async def test_password_is_decrypted_only_after_authenticated_pin_match(self):
        await self._trust_real()
        host = db.get_connection_context(db.get_host(self.hid))
        validator = host_keys.Validator('prod.lan', self.port, 'prod.lan',
                                        credential_host=host)
        with mock.patch.object(crypto, 'decrypt_host_password',
                               return_value='PROD-PASSWORT') as decrypt:
            self.assertIsNone(validator.password_auth_requested())
            self.assertTrue(validator.validate_host_public_key(
                'prod.lan', '127.0.0.1', self.port, self.key_real.convert_to_public()))
            self.assertEqual(validator.password_auth_requested(), 'PROD-PASSWORT')
            decrypt.assert_called_once_with(host['password_enc'], host)

    def test_production_options_ignore_user_ssh_config_routing(self):
        config = self.root / 'config'
        config.write_text(
            'Host prod.lan\n HostName 127.0.0.2\n User attacker\n Port 1\n'
            ' HostKeyAlias attacker\n ProxyJump attacker-hop\n', encoding='utf-8')
        host = db.get_connection_context(db.get_host(self.hid))
        with mock.patch('os.path.expanduser', return_value=str(config)):
            options = ssh_connection.options_for(host)
        self.assertEqual(options.host, 'prod.lan')
        self.assertEqual(options.port, self.port)
        self.assertEqual(options.username, 'root')
        self.assertIsNone(options.host_key_alias)
        self.assertIsNone(options.tunnel)


if __name__ == '__main__':
    unittest.main()
