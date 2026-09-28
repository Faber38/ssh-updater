import importlib.util
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_checks', ROOT / 'scripts/release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTagTests(unittest.TestCase):
    def test_stable_application_version_accepted(self):
        with mock.patch.object(release, 'version', return_value='1.2.4'):
            self.assertEqual(release.validate_tag('v1.2.4'), 'v1.2.4')

    def test_beta_application_cannot_be_released_as_stable(self):
        with mock.patch.object(release, 'version', return_value='1.2.5-beta'):
            for tag in ('v1.2.5', 'v1.2.4'):
                with self.subTest(tag=tag), self.assertRaises(ValueError):
                    release.validate_tag(tag)

    def test_stable_and_beta_tags_require_exact_version_match(self):
        for current in ('1.2.5', '1.2.5-beta', '2.0.0', '2.0.0-beta'):
            with self.subTest(version=current), mock.patch.object(release, 'version', return_value=current):
                self.assertEqual(release.validate_tag('v' + current), 'v' + current)
                other = current.removesuffix('-beta') if current.endswith('-beta') else current + '-beta'
                with self.assertRaises(ValueError):
                    release.validate_tag('v' + other)

    def test_shell_metacharacters_path_and_malformed_tags_rejected(self):
        with mock.patch.object(release, 'version', return_value='1.2.3'):
            for tag in ('v1.2.3;echo injected', 'v1.2.3$(id)', 'v1.2.3`id`',
                        'v1.2.3\n', 'v1.2.3/../x', 'v1.2.3-rc1', 'v01.2.3',
                        '1.2.3', '', 'v1.2', 'v1.2.4', 'v1.2.3-beta2',
                        'v1.2.3-test', 'v1.2.3-irgendwas', 'latest', 'test', 'foo'):
                with self.subTest(tag=tag), self.assertRaises(ValueError):
                    release.validate_tag(tag)

    def test_environment_rejects_wrong_or_missing_asyncssh_provenance(self):
        import json
        from types import SimpleNamespace
        distribution = release.importlib.metadata.distribution
        original = distribution('asyncssh')
        correct = json.loads(original.read_text('direct_url.json'))
        wrong_hash = json.loads(json.dumps(correct))
        wrong_hash['archive_info']['hashes']['sha256'] = '0' * 64
        wrong_url = dict(correct, url='https://example.invalid/other.tar.gz')
        for metadata in (None, json.dumps(wrong_hash), json.dumps(wrong_url)):
            def get(name):
                if name == 'asyncssh':
                    return SimpleNamespace(version=original.version, read_text=lambda _: metadata)
                return distribution(name)
            with mock.patch.object(release.importlib.metadata, 'distribution', side_effect=get):
                with self.assertRaisesRegex(ValueError, 'archive/hash'):
                    release.check_environment()


class WorkflowModeTests(unittest.TestCase):
    sha = 'a' * 40

    def test_manual_main_build_has_version_and_commit_but_no_release_tag(self):
        result = release.validate_workflow('workflow_dispatch', 'refs/heads/main', self.sha)
        self.assertEqual(result['artifact_label'], f'ci-{release.version()}-{self.sha[:12]}')
        self.assertEqual(result['release_tag'], '')

    def test_stable_and_beta_manual_builds_preserve_version(self):
        for current in ('1.2.4', '1.2.5-beta'):
            with self.subTest(version=current), mock.patch.object(release, 'version', return_value=current):
                self.assertEqual(
                    release.validate_workflow('workflow_dispatch', 'refs/heads/main', self.sha),
                    {'artifact_label': f'ci-{current}-{self.sha[:12]}', 'release_tag': ''})

    @mock.patch.object(release, 'version', return_value='1.2.4')
    def test_tag_push_preserves_release_names_and_checks_version(self, _version):
        tag = 'v' + release.version()
        result = release.validate_workflow('push', 'refs/tags/' + tag, self.sha)
        self.assertEqual(result, {'artifact_label': tag, 'release_tag': tag})
        for ref in ('refs/tags/v99.99.99', 'refs/tags/v1.2.3;echo bad',
                    'refs/tags/v1.2.3\nINJECTED=yes', 'refs/heads/v1.2.3'):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                release.validate_workflow('push', ref, self.sha)

    def test_other_events_refs_and_invalid_versions_fail_closed(self):
        for event, ref in [('workflow_dispatch', 'refs/tags/v1.2.3'),
                           ('workflow_dispatch', 'refs/heads/other'),
                           ('push', 'refs/heads/main'), ('pull_request', 'refs/heads/main')]:
            with self.subTest(event=event, ref=ref), self.assertRaises(ValueError):
                release.validate_workflow(event, ref, self.sha)
        for version in ('1.2', '01.2.3', '1.2.3\nINJECTED=yes',
                        '1.2.5-beta\nINJECTED=yes', '1.2.5-beta/../x', None):
            with mock.patch.object(release, 'version', return_value=version):
                with self.assertRaises(ValueError):
                    release.validate_workflow('workflow_dispatch', 'refs/heads/main', self.sha)
        with self.assertRaises(ValueError):
            release.validate_workflow('workflow_dispatch', 'refs/heads/main', 'bad\nvalue')

    def test_actual_cli_outputs_for_both_modes(self):
        import os
        import subprocess
        import sys
        import tempfile
        for event, ref, tag in [('workflow_dispatch', 'refs/heads/main', ''),
                                ('push', 'refs/tags/v' + release.version(), 'v' + release.version())]:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / 'outputs'
                env = dict(os.environ, GITHUB_EVENT_NAME=event, GITHUB_REF=ref,
                           GITHUB_SHA=self.sha, GITHUB_OUTPUT=str(output))
                proc = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/release.py'), 'workflow'],
                                      env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                values = dict(line.split('=', 1) for line in output.read_text(encoding="utf-8").splitlines())
                self.assertEqual(values['release_tag'], tag)
                self.assertEqual(set(values), {'artifact_label', 'release_tag'})

    def test_release_prerelease_flag_uses_only_validated_beta_tag(self):
        workflow = (ROOT / '.github/workflows/build-release.yml').read_text(encoding='utf-8')
        self.assertIn("prerelease: ${{ endsWith(needs.build-linux.outputs.release_tag, '-beta') }}", workflow)
        self.assertIn('name: SSH Updater ${{ needs.build-linux.outputs.release_tag }}', workflow)
        for current, expected in (('1.2.5', False), ('1.2.5-beta', True),
                                  ('2.0.0', False), ('2.0.0-beta', True)):
            with self.subTest(version=current), mock.patch.object(release, 'version', return_value=current):
                result = release.validate_workflow('push', 'refs/tags/v' + current, self.sha)
                self.assertEqual(result['release_tag'].endswith('-beta'), expected)
                manual = release.validate_workflow('workflow_dispatch', 'refs/heads/main', self.sha)
                self.assertEqual(manual['release_tag'], '')

    def test_release_archives_include_only_binary_and_unchanged_license(self):
        import os
        import subprocess
        import tarfile
        import tempfile
        import zipfile

        workflow = (ROOT / '.github/workflows/build-release.yml').read_text(encoding='utf-8')
        self.assertIn('cp ../LICENSE LICENSE', workflow)
        self.assertIn('tar czf "ssh-updater-${ARTIFACT_LABEL}-linux-x86_64.tar.gz" ssh-updater LICENSE', workflow)
        self.assertIn('Copy-Item -LiteralPath ../LICENSE -Destination LICENSE -ErrorAction Stop', workflow)
        self.assertIn('Compress-Archive -LiteralPath "ssh-updater.exe", "LICENSE"', workflow)
        # Execute the real packaging step for this runner with a harmless fixture binary.
        # The CI matrix exercises tar on Linux and Compress-Archive on Windows.
        windows = os.name == 'nt'
        step = 'Create Windows ZIP' if windows else 'Create Linux archive'
        body = workflow.split(f'      - name: {step}\n', 1)[1].split('\n      - name:', 1)[0]
        command = '\n'.join(line[10:] for line in body.split('        run: |\n', 1)[1].splitlines())
        license_bytes = (ROOT / 'LICENSE').read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'dist').mkdir()
            binary = 'ssh-updater.exe' if windows else 'ssh-updater'
            (root / 'dist' / binary).write_bytes(b'packaging-test-fixture')
            (root / 'dist' / 'unrelated.txt').write_text('Must not be packaged', encoding='utf-8')
            (root / 'LICENSE').write_bytes(license_bytes)
            for label in ('v1.2.5', 'v1.2.5-beta'):
                env = dict(os.environ, ARTIFACT_LABEL=label)
                args = (['powershell', '-NoProfile', '-NonInteractive', '-Command',
                         "$ErrorActionPreference = 'Stop'\n" + command] if windows else
                        ['bash', '-e', '-c', command])
                proc = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                if windows:
                    with zipfile.ZipFile(root / 'dist' / f'ssh-updater-{label}-windows-x86_64.zip') as archive:
                        self.assertEqual(set(archive.namelist()), {binary, 'LICENSE'})
                        self.assertEqual(archive.read('LICENSE'), license_bytes)
                else:
                    with tarfile.open(root / 'dist' / f'ssh-updater-{label}-linux-x86_64.tar.gz') as archive:
                        self.assertEqual(set(archive.getnames()), {binary, 'LICENSE'})
                        self.assertEqual(archive.extractfile('LICENSE').read(), license_bytes)

    def test_workflow_trigger_release_gate_permissions_and_shared_checks(self):
        # Structural contract; YAML syntax is also checked separately before commit.
        import re
        workflow = (ROOT / '.github/workflows/build-release.yml').read_text(encoding="utf-8")
        self.assertIn('on:\n  workflow_dispatch:\n  push:\n    tags:\n      - "v*"', workflow)
        self.assertEqual(workflow.count('contents: write'), 1)
        self.assertIn('permissions:\n  contents: read\n', workflow)
        jobs = dict(re.findall(r'^  (build-linux|build-windows|release):\n(.*?)(?=^  [a-z][\w-]*:|\Z)',
                               workflow, re.MULTILINE | re.DOTALL))
        gate = re.search(r'    if: >-\n(.*?)    permissions:', jobs['release'], re.DOTALL).group(1)
        self.assertEqual(' '.join(gate.split()),
                         "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v') && "
                         "needs.build-linux.outputs.release_tag != '' && "
                         "needs.build-linux.outputs.release_tag == needs.build-windows.outputs.release_tag")
        self.assertIn('needs: [build-linux, build-windows]', jobs['release'])
        self.assertIn('permissions:\n      contents: write', jobs['release'])
        for name in ('build-linux', 'build-windows'):
            job = jobs[name]
            self.assertNotIn('contents: write', job)
            self.assertNotIn('    if:', job)
            self.assertIn('release_tag: ${{ steps.validate.outputs.release_tag }}', job)
            for command in ('python scripts/release.py workflow',
                            'python -m pip install -r requirements-build.txt',
                            'python scripts/release.py environment',
                            'python -B scripts/test_release.py',
                            'python -m PyInstaller ssh-updater.spec --noconfirm --clean',
                            '--smoke-test'):
                self.assertIn(command, job)
            self.assertIn('ARTIFACT_LABEL: ${{ steps.validate.outputs.artifact_label }}', job)
            self.assertNotIn('github.ref_name', job)

    def test_external_actions_are_pinned_to_verified_release_commits(self):
        import re
        workflow = (ROOT / '.github/workflows/build-release.yml').read_text(encoding='utf-8')
        # Verified against refs/tags/<version> in each official action repository.
        expected = {
            'actions/checkout': ('11bd71901bbe5b1630ceea73d27597364c9af683', 'v4.2.2'),
            'actions/setup-python': ('a26af69be951a213d495a4c3e4e4022e16d87065', 'v5.6.0'),
            'actions/upload-artifact': ('ea165f8d65b6e75b540449e92b4886f43607fa02', 'v4.6.2'),
            'actions/download-artifact': ('d3f86a106a0bac45b974a628896c90dbdf5c8093', 'v4.3.0'),
            'softprops/action-gh-release': ('6cbd405e2c4e67a21c47fa9e383d020e4e28b836', 'v2.3.3'),
        }
        lines = [line.strip() for line in workflow.splitlines() if 'uses:' in line]
        self.assertEqual(len(lines), 8)
        seen = set()
        for line in lines:
            match = re.fullmatch(r'uses: ([\w-]+/[\w-]+)@([0-9a-f]{40}) # (v\d+\.\d+\.\d+)', line)
            self.assertIsNotNone(match, line)
            action, sha, version = match.groups()
            self.assertEqual((sha, version), expected[action])
            seen.add(action)
        self.assertEqual(seen, set(expected))

    def test_project_text_reads_explicitly_use_utf8(self):
        original = Path.read_text
        def require_utf8(path, *args, **kwargs):
            if path.is_relative_to(ROOT):
                self.assertEqual(kwargs.get('encoding'), 'utf-8', str(path))
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'read_text', require_utf8), mock.patch('builtins.print'):
            self.assertRegex(release.version(), r'^\d+\.\d+\.\d+(?:-beta)?$')
            release.check_environment()
            self.test_workflow_trigger_release_gate_permissions_and_shared_checks()
