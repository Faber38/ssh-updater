import importlib
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class UpdateHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._home = tempfile.TemporaryDirectory()
        cls._home_patch = mock.patch.object(
            Path, "home", classmethod(lambda _cls: Path(cls._home.name))
        )
        cls._home_patch.start()

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        cls.db = importlib.import_module("sshupdater.core.db")

    @classmethod
    def tearDownClass(cls):
        for handler in logging.getLogger().handlers[:]:
            handler.close()
            logging.getLogger().removeHandler(handler)
        cls._home_patch.stop()
        cls._home.cleanup()

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self._db_path = mock.patch.object(self.db, "DB_PATH", Path(self._tempdir.name) / "test.db")
        self._db_path.start()
        self.db.init_db()

        from cryptography.fernet import Fernet
        self._fernet = mock.patch.object(self.db.crypto, '_FERNET', Fernet(Fernet.generate_key()))
        self._fernet.start()

    def tearDown(self):
        self._fernet.stop()
        self._db_path.stop()
        self._tempdir.cleanup()

    def _insert_host(self):
        con = self.db._connect()
        cur = con.execute(
            """
            INSERT INTO hosts(
                proxmox_uid, name, primary_ip, ips_json, port, user,
                auth_method, key_path, password_enc, distro, tags_json,
                last_check, pending_updates
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "node/100",
                "alter-name",
                "192.0.2.10",
                '["192.0.2.10", "192.0.2.11"]',
                22,
                "admin",
                "password",
                None,
                None,
                "debian",
                '["production", "database"]',
                "2026-08-31 12:00:00",
                7,
            ),
        )
        host_id = cur.lastrowid
        con.commit()
        con.close()
        self.db.set_host_password(host_id, "altes-passwort")
        return host_id

    def test_rename_updates_same_record_and_preserves_unedited_fields(self):
        host_id = self._insert_host()

        self.db.update_host(
            host_id,
            name="neuer-name",
            primary_ip="192.0.2.20",
            port=2222,
            user="root",
            auth_method="key",
            key_path="/keys/server",
            password_plain="neues-passwort",
        )

        hosts = self.db.list_hosts()
        self.assertEqual(len(hosts), 1)
        self.assertEqual(hosts[0]["id"], host_id)
        self.assertEqual(hosts[0]["name"], "neuer-name")
        self.assertEqual(self.db.get_host_password(host_id), "neues-passwort")
        self.assertEqual(hosts[0]["ips_json"], '["192.0.2.10", "192.0.2.11"]')
        self.assertEqual(hosts[0]["tags_json"], '["production", "database"]')
        self.assertEqual(hosts[0]["pending_updates"], 7)
        self.assertEqual(hosts[0]["last_check"], "2026-08-31 12:00:00")

    def test_empty_password_preserves_existing_password(self):
        host_id = self._insert_host()

        self.db.update_host(
            host_id,
            name="alter-name",
            primary_ip="192.0.2.10",
            port=22,
            user="admin",
            auth_method="password",
            key_path=None,
            password_plain=None,
        )

        self.assertEqual(self.db.get_host_password(host_id), "altes-passwort")

    def test_missing_id_raises_controlled_error(self):
        with self.assertRaises(self.db.HostNotFoundError):
            self.db.update_host(
                9999,
                name="nicht-vorhanden",
                primary_ip="192.0.2.99",
                port=22,
                user="root",
                auth_method="key",
                key_path=None,
                password_plain=None,
            )


if __name__ == "__main__":
    unittest.main()
