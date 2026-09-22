"""Run tests with isolated home lookups and Qt settings, never a user's vault."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def main():
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    with tempfile.TemporaryDirectory(prefix='ssh-updater-tests-') as tmp:
        root = Path(tmp)
        runtime = root / 'runtime'
        runtime.mkdir(mode=0o700)
        original_expanduser = os.path.expanduser

        def expanduser(path):
            value = os.fspath(path)
            if value == '~' or value.startswith('~/'):
                return str(root) + value[1:]
            return original_expanduser(path)

        with mock.patch.object(Path, 'home', return_value=root), \
                mock.patch('os.path.expanduser', side_effect=expanduser), \
                mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': tmp, 'XDG_CACHE_HOME': tmp,
                                            'XDG_RUNTIME_DIR': str(runtime)}):
            from PyQt6.QtCore import QSettings
            QSettings.setDefaultFormat(QSettings.Format.IniFormat)
            for fmt in (QSettings.Format.NativeFormat, QSettings.Format.IniFormat):
                QSettings.setPath(fmt, QSettings.Scope.UserScope, tmp)
            suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
