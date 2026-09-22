"""Offline artifact check: no home files, credentials, SSH agent or connections."""
import json
import sqlite3
import sys
from PyQt6 import QtCore, QtWidgets
import asyncssh
import cryptography
from cryptography.fernet import Fernet
from . import __version__
from .core import ssh_connection, remote_process
from .ui_text import PlainTextLog


def run(resource_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    log = PlainTextLog()
    sample = '<b>test</b> &amp; <img src="data:image/svg+xml,invalid">'
    log.append(sample)
    assert log.toPlainText() == sample
    assert resource_path('assets', 'qss', 'light.qss').is_file()
    opts = ssh_connection.options_for(
        {'primary_ip': 'example.invalid', 'user': 'test'}, inspect=True, config=[])
    assert opts.x11_forwarding is False and not opts.agent_forward_path
    assert not opts.password_auth and not opts.public_key_auth
    fernet = Fernet(Fernet.generate_key())
    assert fernet.decrypt(fernet.encrypt(b'synthetic smoke test')) == b'synthetic smoke test'
    with sqlite3.connect(':memory:') as con:
        assert con.execute('SELECT 1').fetchone()[0] == 1
    assert remote_process.STREAM_LIMIT > 0
    app.processEvents()
    print(json.dumps({'version': __version__, 'python': sys.version.split()[0],
                      'pyqt': QtCore.PYQT_VERSION_STR, 'qt': QtCore.qVersion(),
                      'asyncssh': asyncssh.__version__, 'cryptography': cryptography.__version__,
                      'smoke': 'ok'}))
    return 0
