import asyncio
from contextlib import asynccontextmanager
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sshupdater.core import remote_process as rp, ssh_client


class Reader:
    def __init__(self, data=b'', hang=False):
        self.data, self.hang = data, hang

    async def read(self, count):
        if self.data:
            chunk, self.data = self.data[:count], self.data[count:]
            return chunk
        if self.hang:
            await asyncio.Event().wait()
        return b''


def connection(stdout=b'', stderr=b'', hang=False, exit_status=0):
    proc = SimpleNamespace(stdout=Reader(stdout, hang), stderr=Reader(stderr),
                           exit_status=exit_status, close=mock.Mock(),
                           wait_closed=mock.AsyncMock(), terminate=mock.Mock(), kill=mock.Mock())
    return SimpleNamespace(create_process=mock.AsyncMock(return_value=proc)), proc


class RemoteProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_drains_both_streams_and_preserves_literal_text(self):
        conn, proc = connection(b'<b>hello</b>\n', b'error\xff', exit_status=7)
        self.assertEqual(await rp.capture(conn, 'synthetic', 1),
                         (7, '<b>hello</b>\n', 'error\ufffd'))
        proc.close.assert_called_once()
        self.assertIs(conn.create_process.call_args.kwargs['request_pty'], False)

    async def test_idle_timeout_covers_process_request_and_channel_close(self):
        for phase in ('request', 'close'):
            with self.subTest(phase=phase):
                conn, proc = connection()
                entered = asyncio.Event()
                async def hang(*args, **kwargs):
                    entered.set()
                    await asyncio.Event().wait()
                if phase == 'request':
                    conn.create_process.side_effect = hang
                else:
                    proc.wait_closed.side_effect = hang
                start = asyncio.get_running_loop().time()
                with self.assertRaises(rp.RemoteTimeoutError):
                    async for _ in rp.output(conn, 'test', timeout=2, limit=100,
                                             idle_timeout=.02):
                        pass
                self.assertTrue(entered.is_set())
                self.assertLess(asyncio.get_running_loop().time() - start, .5)
                proc.kill.assert_not_called()
                proc.terminate.assert_not_called()

    async def test_total_deadline_also_covers_request_and_channel_close(self):
        for phase in ('request', 'close'):
            conn, proc = connection()
            async def hang(*args, **kwargs):
                await asyncio.Event().wait()
            if phase == 'request':
                conn.create_process.side_effect = hang
            else:
                proc.wait_closed.side_effect = hang
            with self.assertRaisesRegex(rp.RemoteTimeoutError, 'Zeitüberschreitung'):
                async for _ in rp.output(conn, 'test', timeout=.02, limit=100,
                                         idle_timeout=2):
                    pass

    async def test_combined_output_limit(self):
        conn, proc = connection(b'a' * 800, b'b' * 800)
        with mock.patch.object(rp, 'CAPTURE_LIMIT', 1000):
            with self.assertRaisesRegex(rp.RemoteWaitError, 'Ausgabelimit'):
                await rp.capture(conn, 'synthetic', 1)
        proc.close.assert_called_once()
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    async def test_hanging_process_total_timeout(self):
        conn, proc = connection(hang=True)
        with self.assertRaisesRegex(rp.RemoteWaitError, 'Zeitüberschreitung'):
            await rp.capture(conn, 'synthetic', .02)
        proc.close.assert_called_once()
        proc.kill.assert_not_called()

    async def test_idle_timeout(self):
        conn, _ = connection(hang=True)
        with self.assertRaisesRegex(rp.RemoteWaitError, 'Keine Remote-Ausgabe'):
            async for _ in rp.output(conn, 'synthetic', timeout=1, limit=100, idle_timeout=.01):
                pass

    async def test_stream_timeout_argument_and_unknown_admin_result(self):
        conn, _ = connection(hang=True)
        with self.assertRaises(rp.RemoteWaitError):
            _ = [x async for x in ssh_client._stream(conn, 'synthetic', timeout=.02)]
        @asynccontextmanager
        async def connected(_):
            yield conn
        async def failing(*_):
            raise rp.RemoteWaitError('synthetic timeout')
            yield
        with mock.patch.object(ssh_client, 'connect_host', connected), \
                mock.patch.object(ssh_client, '_detect_distro', mock.AsyncMock(return_value='debian')), \
                mock.patch.object(ssh_client, '_stream', failing):
            for operation in (ssh_client.upgrade_host_stream, ssh_client.autoremove_host_stream):
                result = [x async for x in operation({'id': 1, 'primary_ip': 'test', 'user': 'root'})]
                self.assertEqual(result[-1]['result']['status'], 'unknown')
                self.assertIn('Remote-Zustand unbekannt', result[-1]['result']['note'])

    async def test_long_line_bounded_and_remote_exit_marker_is_only_text(self):
        conn, _ = connection(b'x' * 100000 + b'\n[RC=0]\n', exit_status=9)
        events = [x async for x in rp.stream(conn, 'synthetic', 1)]
        self.assertEqual(events[-1], rp.CommandExit(9))
        self.assertIn('[RC=0]', events)
        self.assertTrue(all(len(x) <= 4096 for x in events[:-1]))

    async def test_user_cancellation_closes_channel_without_kill(self):
        conn, proc = connection(hang=True)
        task = asyncio.create_task(rp.capture(conn, 'synthetic', 10))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        proc.close.assert_called_once()
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    async def test_missing_exit_status_is_not_success(self):
        conn, _ = connection(exit_status=None)
        with self.assertRaisesRegex(rp.RemoteWaitError, 'Exitcode'):
            await rp.capture(conn, 'synthetic', 1)
