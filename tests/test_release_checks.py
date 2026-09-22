import importlib.util
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_checks', ROOT / 'scripts/release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTagTests(unittest.TestCase):
    def test_current_application_version_accepted(self):
        tag = 'v' + release.version()
        self.assertEqual(release.validate_tag(tag), tag)

    def test_shell_metacharacters_path_and_malformed_tags_rejected(self):
        with mock.patch.object(release, 'version', return_value='1.2.3'):
            for tag in ('v1.2.3;echo injected', 'v1.2.3$(id)', 'v1.2.3`id`',
                        'v1.2.3\n', 'v1.2.3/../x', 'v1.2.3-rc1', 'v01.2.3',
                        '1.2.3', '', 'v1.2', 'v1.2.4'):
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

    def test_tag_push_preserves_release_names_and_checks_version(self):
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
        for version in ('1.2', '01.2.3', '1.2.3\nINJECTED=yes', None):
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

    def test_project_text_reads_explicitly_use_utf8(self):
        original = Path.read_text
        def require_utf8(path, *args, **kwargs):
            if path.is_relative_to(ROOT):
                self.assertEqual(kwargs.get('encoding'), 'utf-8', str(path))
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'read_text', require_utf8), mock.patch('builtins.print'):
            self.assertRegex(release.version(), r'^\d+\.\d+\.\d+$')
            release.check_environment()
            self.test_workflow_trigger_release_gate_permissions_and_shared_checks()
