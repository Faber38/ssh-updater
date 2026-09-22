from contextlib import asynccontextmanager
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sshupdater.core import ssh_client as ssh


class SimulationTests(unittest.IsolatedAsyncioTestCase):
    async def test_apt_index_errors_stop_checks_and_simulations(self):
        for operation in (ssh._check_debian, ssh._sim_debian):
            for diagnostic in ('Temporary failure resolving repo.example',
                               'Some index files failed to download; old ones used',
                               'E: The repository does not have a Release file'):
                with self.subTest(operation=operation.__name__, diagnostic=diagnostic):
                    with mock.patch.object(ssh, '_run', mock.AsyncMock(
                            return_value=(100, '', diagnostic))) as run:
                        with self.assertRaises(ssh.UpdateCheckError):
                            await operation(None)
                        self.assertEqual(run.await_count, 1)
                        self.assertIn('update --error-on=any', run.call_args.args[1])

    async def test_apt_success_empty_and_warnings_preserved(self):
        for operation in (ssh._check_debian, ssh._sim_debian):
            for text, count in [('Inst package [1] (2 repo)\n', 1), ('', 0)]:
                with mock.patch.object(ssh, '_run', mock.AsyncMock(side_effect=[
                        (0, '', 'W: index warning'), (0, text, 'W: simulation warning')])) as run:
                    result = await operation(None)
                    self.assertEqual(result[0], count)
                    self.assertIn('index warning', result[-1])
                    self.assertIn('simulation warning', result[-1])
                    self.assertIn('--error-on=any', run.call_args_list[0].args[1])

    async def test_apt_upgrade_index_failure_never_starts_upgrade(self):
        calls = []
        async def stream(conn, command):
            calls.append(command)
            yield 'Temporary failure downloading an index'
            yield ssh.CommandExit(100)
        with mock.patch.object(ssh, '_stream', stream):
            for sudo in (False, True):
                calls.clear()
                result = [x async for x in ssh._upgrade_debian(None, sudo)]
                self.assertEqual(result[-1], ssh.CommandExit(100))
                self.assertEqual(len(calls), 1)
                self.assertIn('update --error-on=any', calls[0])

    async def test_debian_success_and_no_updates(self):
        for text, count in [('Inst package [1] (2 repo)\n', 1), ('', 0)]:
            with mock.patch.object(ssh, '_run', mock.AsyncMock(side_effect=[(0, '', ''), (0, text, '')])):
                self.assertEqual((await ssh._sim_debian(None))[0], count)

    async def test_debian_update_and_simulation_failures(self):
        for responses in [[(100, '', 'failed')], [(124, '', 'timeout')],
                          [(0, '', ''), (100, '', 'failed')], [(0, '', ''), (124, '', 'timeout')]]:
            with mock.patch.object(ssh, '_run', mock.AsyncMock(side_effect=responses)) as run:
                with self.assertRaises(ssh.UpdateCheckError):
                    await ssh._sim_debian(None)
                self.assertEqual(run.await_count, len(responses))

    async def test_rpm_documented_exit_codes(self):
        for code, count in [(0, 0), (100, 1), (1, None), (124, None)]:
            with mock.patch.object(ssh, '_run', mock.AsyncMock(return_value=(code, 'pkg.x86_64 2 repo\n', ''))):
                if count is None:
                    with self.assertRaises(ssh.UpdateCheckError):
                        await ssh._sim_rpm(None)
                else:
                    self.assertEqual((await ssh._sim_rpm(None))[0], count)

    async def test_arch_simulation_documented_exit_codes(self):
        for code, count in [(0, 1), (2, 0), (1, None), (127, None), (124, None)]:
            with mock.patch.object(ssh, '_run', mock.AsyncMock(return_value=(code, 'pkg 1 -> 2\n', ''))):
                if count is None:
                    with self.assertRaises(ssh.UpdateCheckError):
                        await ssh._sim_arch(None)
                else:
                    self.assertEqual((await ssh._sim_arch(None))[0], count)

    async def test_arch_check_exit_two_is_no_updates(self):
        with mock.patch.object(ssh, '_run', mock.AsyncMock(side_effect=[(0, '/bin/checkupdates', ''), (2, '', '')])):
            self.assertEqual(await ssh._check_arch(None), (0, ''))

    async def test_autoremove_success_empty_failure_timeout(self):
        for code, out, count in [(0, 'Remv pkg\n', 1), (0, '', 0), (100, '', None), (124, '', None)]:
            with mock.patch.object(ssh, '_run', mock.AsyncMock(return_value=(code, out, ''))):
                if count is None:
                    with self.assertRaises(ssh.UpdateCheckError):
                        await ssh._sim_autoremove_debian(None)
                else:
                    self.assertEqual((await ssh._sim_autoremove_debian(None))[0], count)

    async def test_callers_report_simulation_failure_and_connection_loss(self):
        @asynccontextmanager
        async def connected(_):
            yield object()
        host = {'id': 1, 'primary_ip': 'test', 'user': 'root'}
        with mock.patch.object(ssh, 'connect_host', connected), \
                mock.patch.object(ssh, '_detect_distro', mock.AsyncMock(return_value='debian')):
            for error in [ssh.UpdateCheckError('failed'), ssh.asyncssh.ConnectionLost('lost')]:
                for operation, method in [(ssh.simulate_upgrade_for_host, '_sim_debian'),
                                          (ssh.simulate_autoremove_for_host, '_sim_autoremove_debian')]:
                    with mock.patch.object(ssh, method, mock.AsyncMock(side_effect=error)):
                        self.assertEqual((await operation(host))['status'], 'error')
