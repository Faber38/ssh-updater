"""Regressionstest fuer F3: speicherharte Ableitung, Mindestlaenge, Migration, Drossel."""
import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from sshupdater.core import crypto, db, storage, credentials


class F3Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for module, name, value in (
            (crypto, 'DATA_DIR', self.root),
            (crypto, '_SALT_PATH', self.root / 'vault.salt'),
            (crypto, '_VERIFIER_PATH', self.root / 'vault.verify'),
            (crypto, '_FERNET', None),
            (crypto, '_MAC_KEY', None),
            (db, 'DB_PATH', self.root / 'app.db'),
        ):
            patch = mock.patch.object(module, name, value)
            patch.start(); self.addCleanup(patch.stop)
        storage.initialize(self.root)

    def _make_legacy_vault(self, password: str):
        salt = os.urandom(16)
        raw = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                         iterations=200_000).derive(password.encode('utf-8'))
        fernet = Fernet(base64.urlsafe_b64encode(raw))
        storage.write_private(crypto._SALT_PATH, salt, exclusive=True)
        storage.write_private(crypto._VERIFIER_PATH, fernet.encrypt(crypto._CHALLENGE), exclusive=True)
        return fernet

    def test_fresh_vault_uses_scrypt(self):
        crypto.set_master_password('correct horse battery staple')
        self.assertTrue(crypto._kdf_path().exists())
        self.assertFalse(crypto._SALT_PATH.exists())
        block = json.loads(crypto._kdf_path().read_bytes().decode('utf-8'))
        self.assertEqual(block['algo'], 'scrypt')
        self.assertGreaterEqual(block['n'], 1 << 15)

    def test_minimum_length_enforced_on_creation_only(self):
        with self.assertRaises(crypto.WeakPassword):
            crypto.set_master_password('short')
        self.assertFalse(crypto._VERIFIER_PATH.exists())
        # Bestehender Vault: kurzes Entsperr-Passwort ist WrongPassword, nicht WeakPassword.
        crypto.set_master_password('correct horse battery staple')
        crypto._FERNET = None
        with self.assertRaises(crypto.WrongPassword):
            crypto.set_master_password('x')

    def test_low_diversity_password_is_rejected(self):
        with self.assertRaises(crypto.WeakPassword):
            crypto.set_master_password('aaaaaaaaaaaa')
        self.assertFalse(crypto._VERIFIER_PATH.exists())

    def test_kdf_policy_downgrade_is_rejected_without_unlocking(self):
        crypto.set_master_password('correct horse battery staple')
        block = json.loads(crypto._kdf_path().read_text(encoding='utf-8'))
        for field, value in [('n', 1 << 10), ('version', True), ('algo', 'pbkdf2')]:
            with self.subTest(field=field):
                modified = dict(block, **{field: value})
                storage.write_private(crypto._kdf_path(), json.dumps(modified).encode('utf-8'))
                crypto._FERNET = None
                crypto._MAC_KEY = None
                with self.assertRaises(OSError):
                    crypto.set_master_password('correct horse battery staple')
                self.assertFalse(crypto.is_unlocked())

    def test_failed_kdf_write_rolls_back_fresh_verifier(self):
        with mock.patch.object(crypto, '_write_kdf', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                crypto.set_master_password('correct horse battery staple')
        self.assertFalse(crypto._VERIFIER_PATH.exists())
        self.assertFalse(crypto.is_unlocked())

    def test_legacy_vault_migrates_and_keeps_credentials(self):
        fernet = self._make_legacy_vault('legacy pass phrase')
        db.init_db()
        host = dict(id=1, primary_ip='srv', user='root', port=22)
        token = credentials.encrypt(fernet, 'legacy-secret', host)
        con = db._connect()
        con.execute("INSERT INTO hosts(id,name,primary_ip,user,port,auth_method,password_enc) "
                    "VALUES(1,'h','srv','root',22,'password',?)", (token,))
        con.commit(); con.close()

        crypto.set_master_password('legacy pass phrase')
        self.assertTrue(crypto._kdf_path().exists())
        self.assertFalse(crypto._SALT_PATH.exists())
        self.assertEqual(db.get_host_password(1), 'legacy-secret')

        # Nach Migration erneut entsperren (scrypt-Pfad).
        crypto._FERNET = None; crypto._MAC_KEY = None
        crypto.set_master_password('legacy pass phrase')
        self.assertEqual(db.get_host_password(1), 'legacy-secret')

    def test_throttle_grows_and_resets(self):
        crypto.set_master_password('correct horse battery staple')
        crypto._FERNET = None
        for _ in range(2):
            with self.assertRaises(crypto.WrongPassword):
                crypto.set_master_password('wrong wrong wrong')
        start = time.time()
        with self.assertRaises(crypto.WrongPassword):
            crypto.set_master_password('wrong wrong wrong')
        self.assertGreaterEqual(time.time() - start, 3.0)
        crypto.set_master_password('correct horse battery staple')
        self.assertFalse(crypto._throttle_path().exists())


if __name__ == '__main__':
    unittest.main()
