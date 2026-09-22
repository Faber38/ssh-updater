"""Small shared release checks; no publishing or git mutations."""
import argparse
import ast
import importlib.metadata
import json
from urllib.parse import urldefrag, parse_qs
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def version():
    tree = ast.parse((ROOT / 'src/sshupdater/__init__.py').read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and
                t.id == '__version__' for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('Application version missing')


def validate_tag(tag):
    if not re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag):
        raise ValueError('Tag must be vMAJOR.MINOR.PATCH')
    if tag != 'v' + version():
        raise ValueError('Tag and application version differ')
    return tag


def check_environment():
    from packaging.requirements import Requirement
    expected = (ROOT / 'release-python.txt').read_text().strip()
    actual = '.'.join(map(str, sys.version_info[:2]))
    if actual != expected:
        raise ValueError(f'Expected Python {expected}.x, got {sys.version.split()[0]}')
    for filename in ('requirements.txt', 'requirements-build.txt'):
        for line in (ROOT / filename).read_text().splitlines():
            if not line or line.startswith(('#', '-r')):
                continue
            req = Requirement(line)
            if req.marker and not req.marker.evaluate():
                continue
            installed = importlib.metadata.version(req.name)
            if req.url:
                url, fragment = urldefrag(req.url)
                expected_hash = parse_qs(fragment).get('sha256', [None])[0]
                metadata = importlib.metadata.distribution(req.name).read_text('direct_url.json')
                direct = json.loads(metadata or '{}')
                actual_hash = direct.get('archive_info', {}).get('hashes', {}).get('sha256')
                if not expected_hash or direct.get('url') != url or actual_hash != expected_hash:
                    raise ValueError(f'{req.name}: pinned source archive/hash does not match installed package')
            if installed not in req.specifier:
                raise ValueError(f'{req.name}: expected {req.specifier}, got {installed}')
    print(f'Release environment verified: Python {sys.version.split()[0]}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('check', choices=['tag', 'environment'])
    args = parser.parse_args()
    if args.check == 'tag':
        print(validate_tag(os.environ.get('RELEASE_TAG', '')))
    else:
        check_environment()
