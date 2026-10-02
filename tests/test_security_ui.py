import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PyQt6 import QtWidgets
from sshupdater.ui_config import HostEditDialog
from sshupdater.ui_text import PlainMessageBox


class SecurityDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def host(self):
        return dict(id=1, name='server', primary_ip='192.0.2.1', port=22,
                    user='admin', auth_method='password', password_enc=b'token')

    def choose(self, text):
        def execute(box):
            for button in box.buttons():
                if button.text() == text:
                    button.click()
                    return 0
            raise AssertionError(text)
        return mock.patch.object(PlainMessageBox, 'exec', execute)

    def test_target_change_cancel_reentry_and_delete(self):
        for choice, accepted in [('Cancel', False), ('Passwort erneut eingeben', False),
                                  ('Passwort löschen und speichern', True)]:
            with self.subTest(choice=choice):
                dialog = HostEditDialog(None, self.host())
                dialog.in_ip.setText('192.0.2.2')
                with self.choose(choice):
                    dialog._save()
                self.assertEqual(dialog.result() == QtWidgets.QDialog.DialogCode.Accepted, accepted)
                if accepted:
                    self.assertTrue(dialog.result_host['delete_password'])
                dialog.deleteLater()

    def test_key_switch_choice(self):
        for choice in ('Löschen (empfohlen)', 'Verschlüsselt behalten', 'Cancel'):
            with self.subTest(choice=choice):
                dialog = HostEditDialog(None, self.host())
                dialog.in_auth.setCurrentText('key')
                with self.choose(choice):
                    dialog._save()
                if choice == 'Cancel':
                    self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Rejected)
                else:
                    self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Accepted)
                    self.assertEqual(dialog.result_host['delete_password'], choice == 'Löschen (empfohlen)')
                    self.assertEqual(dialog.result_host['keep_password'] is True, choice == 'Verschlüsselt behalten')
                dialog.deleteLater()

    def test_reentered_password_allows_new_target(self):
        dialog = HostEditDialog(None, self.host())
        dialog.in_port.setValue(2222)
        dialog.in_pwd.setText('reentered')
        with mock.patch.object(PlainMessageBox, 'exec') as prompt:
            dialog._save()
        prompt.assert_not_called()
        self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Accepted)
        dialog.deleteLater()
