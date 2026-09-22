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
