"""Startup migration and fail-closed truststore regressions."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PyQt6 import QtWidgets
from sshupdater import app as application
from sshupdater.core import host_keys


class SecurityStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_first_run_creates_authenticated_empty_truststore(self):
        window = mock.Mock()
        with mock.patch.object(application.QtWidgets, 'QApplication', return_value=self.qt), \
                mock.patch.object(application.settings, 'initialize'), \
                mock.patch.object(application.crypto, 'keystore_exists', return_value=False), \
                mock.patch.object(application.crypto, 'require_strong_password'), \
                mock.patch.object(application.crypto, 'set_master_password') as unlock, \
                mock.patch.object(application.host_keys, 'initialize_fresh') as initialize, \
                mock.patch.object(application.host_keys, 'load') as load, \
                mock.patch.object(application.QInputDialog, 'getText',
                                  side_effect=[('long master password', True),
                                               ('long master password', True)]), \
                mock.patch.object(application.db, 'init_db'), \
                mock.patch.object(application, 'MainWindow', return_value=window), \
                mock.patch.object(application, 'apply_theme'), \
                mock.patch.object(self.qt, 'exec', return_value=0):
            self.assertEqual(application.main(), 0)
        unlock.assert_called_once_with('long master password')
        initialize.assert_called_once_with()
        load.assert_not_called()
        window.show.assert_called_once_with()

    def test_legacy_truststore_requires_explicit_reconfirmation(self):
        migration = host_keys.TruststoreMigrationRequired('missing integrity tag')
        with mock.patch.object(application.QtWidgets, 'QApplication', return_value=self.qt), \
                mock.patch.object(application.settings, 'initialize'), \
                mock.patch.object(application.crypto, 'keystore_exists', return_value=True), \
                mock.patch.object(application.crypto, 'set_master_password'), \
                mock.patch.object(application.host_keys, 'load', side_effect=migration), \
                mock.patch.object(application.host_keys, 'reset_for_reconfirmation') as reset, \
                mock.patch.object(application.QInputDialog, 'getText',
                                  return_value=('existing master', True)), \
                mock.patch.object(application.QMessageBox, 'warning',
                                  return_value=application.QMessageBox.StandardButton.Cancel), \
                mock.patch.object(application.db, 'init_db') as init_db, \
                mock.patch.object(application, 'apply_theme'):
            self.assertEqual(application.main(), 1)
        reset.assert_not_called()
        init_db.assert_not_called()

    def test_confirmed_legacy_reset_quarantines_before_start(self):
        migration = host_keys.TruststoreMigrationRequired('missing integrity tag')
        window = mock.Mock()
        with mock.patch.object(application.QtWidgets, 'QApplication', return_value=self.qt), \
                mock.patch.object(application.settings, 'initialize'), \
                mock.patch.object(application.crypto, 'keystore_exists', return_value=True), \
                mock.patch.object(application.crypto, 'set_master_password'), \
                mock.patch.object(application.host_keys, 'load', side_effect=migration), \
                mock.patch.object(application.host_keys, 'reset_for_reconfirmation') as reset, \
                mock.patch.object(application.QInputDialog, 'getText',
                                  return_value=('existing master', True)), \
                mock.patch.object(application.QMessageBox, 'warning',
                                  return_value=application.QMessageBox.StandardButton.Yes), \
                mock.patch.object(application.db, 'init_db'), \
                mock.patch.object(application, 'MainWindow', return_value=window), \
                mock.patch.object(application, 'apply_theme'), \
                mock.patch.object(self.qt, 'exec', return_value=0):
            self.assertEqual(application.main(), 0)
        reset.assert_called_once_with()
        window.show.assert_called_once_with()

    def test_integrity_mismatch_stops_without_reset_offer(self):
        error = host_keys.TruststoreIntegrityError('mismatch')
        with mock.patch.object(application.QtWidgets, 'QApplication', return_value=self.qt), \
                mock.patch.object(application.settings, 'initialize'), \
                mock.patch.object(application.crypto, 'keystore_exists', return_value=True), \
                mock.patch.object(application.crypto, 'set_master_password'), \
                mock.patch.object(application.host_keys, 'load', side_effect=error), \
                mock.patch.object(application.host_keys, 'reset_for_reconfirmation') as reset, \
                mock.patch.object(application.QInputDialog, 'getText',
                                  return_value=('existing master', True)), \
                mock.patch.object(application.QMessageBox, 'critical') as critical, \
                mock.patch.object(application.db, 'init_db') as init_db, \
                mock.patch.object(application, 'apply_theme'):
            self.assertEqual(application.main(), 1)
        reset.assert_not_called()
        init_db.assert_not_called()
        critical.assert_called_once()


if __name__ == '__main__':
    unittest.main()
