import asyncio
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PyQt6 import QtCore, QtWidgets
from sshupdater import ui_main
from sshupdater.core import db, ssh_client
from test_remote_process import connection

HOSTS = [dict(id=i, name=f'host-{i}', primary_ip='test', user='root') for i in (1, 2)]


class ActionLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_timeout_on_first_host_does_not_block_next_host(self):
        for worker_cls, method in [(ui_main._UpgradeWorker, 'upgrade_host_stream'),
                                   (ui_main._CleanRunWorker, 'autoremove_host_stream')]:
            worker = worker_cls([1, 2])
            done = []
            worker.host_done.connect(done.append)

            async def action(host):
                conn, _ = connection(hang=host['id'] == 1)
                code, _, err = await ssh_client._run(conn, 'synthetic', timeout=.02)
                yield {'type': 'result', 'result': {'status': 'error' if code else 'ok', 'note': err}}

            with mock.patch.object(db, 'list_hosts', return_value=HOSTS), \
                    mock.patch.object(ssh_client, method, action):
                worker.run()
            self.assertEqual([result['status'] for result in done], ['error', 'ok'])
            self.assertEqual([result['host_id'] for result in done], [1, 2])

    def test_actual_thread_stop_ends_local_wait_and_skips_next_host(self):
        for worker_cls, method in [(ui_main._UpgradeWorker, 'upgrade_host_stream'),
                                   (ui_main._CleanRunWorker, 'autoremove_host_stream')]:
            worker = worker_cls([1, 2])
            done, started, closed = [], [], []
            loop = QtCore.QEventLoop()

            async def action(host):
                started.append(host['id'])
                try:
                    yield {'type': 'line', 'line': 'waiting'}
                    await asyncio.Event().wait()
                finally:
                    closed.append(host['id'])

            def progress(_):
                worker.request_stop()

            worker.progress.connect(progress)
            worker.host_done.connect(done.append)
            worker.finished.connect(loop.quit)
            timer = QtCore.QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(loop.quit)
            with mock.patch.object(db, 'list_hosts', return_value=HOSTS), \
                    mock.patch.object(ssh_client, method, action):
                timer.start(3000)
                worker.start()
                loop.exec()
                completed = worker.wait(100)
                if not completed:
                    worker.request_stop()
                    worker.wait(3000)
                self.app.processEvents()
            timer.stop()
            self.assertTrue(completed)
            self.assertEqual(started, [1])
            self.assertEqual(closed, [1])
            self.assertEqual(done[-1]['status'], 'unknown')
            self.assertIn('Remote-Zustand unbekannt', done[-1]['note'])

    def test_streaming_lines_are_batched(self):
        worker = ui_main._UpgradeWorker([1])
        batches = []
        worker.progress.connect(batches.append)

        async def action(_):
            for _ in range(10000):
                yield {'type': 'line', 'line': 'line'}
            yield {'type': 'result', 'result': {'status': 'ok'}}

        with mock.patch.object(db, 'list_hosts', return_value=HOSTS[:1]), \
                mock.patch.object(ssh_client, 'upgrade_host_stream', action):
            worker.run()
        self.assertLess(len(batches), 200)
        self.assertEqual(sum(len(x['line'].splitlines()) for x in batches), 10000)

    def test_stop_all_single_result_workers_and_skip_later_hosts(self):
        import threading
        cases = [(ui_main._CheckWorker, 'check_updates_for_host', 'one_result'),
                 (ui_main._SimWorker, 'simulate_upgrade_for_host', 'one_result'),
                 (ui_main._CleanSimWorker, 'simulate_autoremove_for_host', 'one_result'),
                 (ui_main._RebootWorker, 'reboot_host', 'host_done')]
        for worker_cls, method, signal in cases:
            with self.subTest(worker=worker_cls.__name__):
                worker = worker_cls([1, 2])
                entered = threading.Event()
                started, closed, results = [], [], []
                getattr(worker, signal).connect(results.append)
                async def operation(host):
                    started.append(host['id'])
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        closed.append(host['id'])
                with mock.patch.object(db, 'list_hosts', return_value=HOSTS), \
                        mock.patch.object(ssh_client, method, operation):
                    worker.start()
                    ready = entered.wait(2)
                    worker.request_stop()
                    completed = worker.wait(3000)
                    self.app.processEvents()
                self.assertTrue(ready)
                self.assertTrue(completed)
                self.assertEqual(started, [1])
                self.assertEqual(closed, [1])
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]['status'], 'unknown')
                self.assertIn('Remote-Zustand unbekannt', results[0]['note'])

    def test_single_result_timeout_allows_next_host(self):
        cases = [(ui_main._CheckWorker, 'check_updates_for_host', 'one_result'),
                 (ui_main._SimWorker, 'simulate_upgrade_for_host', 'one_result'),
                 (ui_main._CleanSimWorker, 'simulate_autoremove_for_host', 'one_result'),
                 (ui_main._RebootWorker, 'reboot_host', 'host_done')]
        for worker_cls, method, signal in cases:
            worker = worker_cls([1, 2])
            results = []
            getattr(worker, signal).connect(results.append)
            async def operation(host):
                conn, _ = connection(hang=host['id'] == 1)
                code, _, err = await ssh_client._run(conn, 'test', .02)
                return dict(host_id=host['id'], status='error' if code else 'ok', note=err)
            with mock.patch.object(db, 'list_hosts', return_value=HOSTS), \
                    mock.patch.object(ssh_client, method, operation):
                worker.run()
            self.assertEqual([r['status'] for r in results], ['error', 'ok'])
