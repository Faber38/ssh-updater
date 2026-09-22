"""Non-destructive handling of missing, interrupted and legacy vault state."""
from contextlib import closing
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sshupdater.core import crypto, db, storage


class VaultInitializationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for module, name, value in (
                (crypto, 'DATA_DIR', self.root),
                (crypto, '_SALT_PATH', self.root / 'vault.salt'),
                (crypto, '_VERIFIER_PATH', self.root / 'vault.verify'),
                (crypto, '_FERNET', None), (db, 'DB_PATH', self.root / 'app.db')):
            patch = mock.patch.object(module, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def create_credentials(self):
        crypto.set_master_password('original')
        db.init_db()
        self.hid = db.add_or_update_host(proxmox_uid=None, name='old', primary_ip='server',
                                        auth_method='password', password_plain='retained secret')

    def assert_rejected_unchanged(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        with self.assertRaises(OSError):
            crypto.keystore_exists()
        with self.assertRaises(OSError):
            crypto.set_master_password('replacement')
        self.assertFalse(crypto.is_unlocked())
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})

    def test_both_files_missing_with_credentials(self):
        self.create_credentials()
        crypto._SALT_PATH.unlink()
        crypto._VERIFIER_PATH.unlink()
        self.assert_rejected_unchanged()

    def test_only_salt_missing_with_credentials(self):
        self.create_credentials()
        crypto._SALT_PATH.unlink()
        self.assert_rejected_unchanged()

    def test_only_verifier_missing_with_credentials(self):
        self.create_credentials()
        crypto._VERIFIER_PATH.unlink()
        self.assert_rejected_unchanged()

    def test_interrupted_first_initialization_fails_closed_on_restart(self):
        original = storage.write_private
        def fail_verifier(path, data, **kwargs):
            if path == crypto._VERIFIER_PATH:
                raise OSError('simulated interrupted write')
            return original(path, data, **kwargs)
        with mock.patch.object(storage, 'write_private', side_effect=fail_verifier):
            with self.assertRaises(OSError):
                crypto.set_master_password('original')
        self.assertTrue(crypto._SALT_PATH.exists())
        self.assert_rejected_unchanged()

    def test_fresh_installation_and_subsequent_unlock(self):
        self.assertFalse(crypto.keystore_exists())
        crypto.set_master_password('original')
        encrypted = crypto.encrypt_str('retained secret')
        before = (crypto._SALT_PATH.read_bytes(), crypto._VERIFIER_PATH.read_bytes())
        crypto.set_master_password('original')
        self.assertEqual(crypto.decrypt_str(encrypted), 'retained secret')
        self.assertEqual(before, (crypto._SALT_PATH.read_bytes(), crypto._VERIFIER_PATH.read_bytes()))

    def test_existing_database_without_credentials_allows_initialization(self):
        db.init_db()
        db.add_or_update_host(proxmox_uid=None, name='key only', primary_ip='server')
        self.assertFalse(crypto.keystore_exists())
        crypto.set_master_password('original')
        self.assertEqual(len(db.list_hosts()), 1)

    def test_encrypted_config_without_vault_is_preserved(self):
        (self.root / 'config.enc').write_bytes(b'previous encrypted configuration')
        self.assert_rejected_unchanged()

    def test_corrupt_database_never_creates_vault(self):
        (self.root / 'app.db').write_bytes(b'not a database')
        self.assert_rejected_unchanged()

    def test_invalid_salt_is_preserved(self):
        self.create_credentials()
        crypto._SALT_PATH.write_bytes(b'truncated')
        self.assert_rejected_unchanged()

    def test_mismatched_credentials_do_not_unlock_or_modify_data(self):
        self.create_credentials()
        with closing(sqlite3.connect(self.root / 'app.db')) as con, con:
            con.execute('UPDATE hosts SET password_enc=?', (b'foreign or corrupt token',))
        before = (self.root / 'app.db').read_bytes()
        with self.assertRaisesRegex(OSError, 'inkonsistent'):
            crypto.set_master_password('original')
        self.assertFalse(crypto.is_unlocked())
        self.assertEqual((self.root / 'app.db').read_bytes(), before)
        with self.assertRaises(sqlite3.ProgrammingError):
            con.execute('SELECT 1')
        moved = (self.root / 'app.db').rename(self.root / 'renamed.db')
        moved.unlink()

    def test_damaged_verifier_does_not_unlock_or_replace_files(self):
        self.create_credentials()
        crypto._VERIFIER_PATH.write_bytes(b'partial verifier')
        with self.assertRaisesRegex(crypto.WrongPassword, 'beschädigt'):
            crypto.set_master_password('original')
        self.assertFalse(crypto.is_unlocked())
        self.assertEqual(crypto._VERIFIER_PATH.read_bytes(), b'partial verifier')

    def test_credentials_in_wal_prevent_new_vault(self):
        db.init_db()
        con = sqlite3.connect(self.root / 'app.db')
        self.addCleanup(con.close)
        con.execute('PRAGMA journal_mode=WAL')
        con.execute("INSERT INTO hosts(name, primary_ip, password_enc) VALUES('old', 'server', ?)", (b'token',))
        con.commit()
        with self.assertRaises(OSError):
            crypto.set_master_password('replacement')
        self.assertFalse(crypto._SALT_PATH.exists())
        self.assertEqual(con.execute('SELECT password_enc FROM hosts').fetchone()[0], b'token')
