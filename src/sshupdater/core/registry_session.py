"""Disposable registry metadata only: no filesystem, credentials or host results."""
from collections import OrderedDict
from copy import deepcopy
from threading import RLock
from time import monotonic
import re

# Explicit public repositories; unknown/private repositories remain host-scoped.
PUBLIC_REPOSITORIES = frozenset(('docker.io/library/nginx', 'docker.io/library/httpd',
                                 'docker.io/library/registry'))


def snapshot(value):
    from .docker_image_updates import descriptor, INDEX_TYPES, CheckFailure
    result = descriptor(value)
    if result['mediaType'] in INDEX_TYPES:
        entries = value.get('manifests', [])
        if not isinstance(entries, list) or len(entries) > 1024:
            raise CheckFailure('invalid_output')
        result['manifests'] = []
        for entry in entries:
            child = descriptor(entry)
            platform = entry.get('platform', {})
            if not isinstance(platform, dict):
                raise CheckFailure('invalid_output')
            child['platform'] = {}
            for field in ('os', 'architecture', 'variant'):
                if field in platform:
                    part = platform[field]
                    if not isinstance(part, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', part):
                        raise CheckFailure('invalid_output')
                    child['platform'][field] = part
            annotations = entry.get('annotations', {})
            if not isinstance(annotations, dict):
                raise CheckFailure('invalid_output')
            if annotations.get('vnd.docker.reference.type') == 'attestation-manifest':
                child['annotations'] = {'vnd.docker.reference.type': 'attestation-manifest'}
            result['manifests'].append(child)
    return result


class RegistrySession:
    TTL = 30 * 60

    def __init__(self, *, clock=monotonic):
        self._clock = clock
        self._entries = OrderedDict()
        self._limits = {}
        self._lock = RLock()

    def keys(self, ref, host_scope):
        from .docker_image_updates import reference
        ref = reference(ref)
        repository = ref.rsplit(':', 1)[0]
        scope = 'public' if repository in PUBLIC_REPOSITORIES else host_scope
        registry = ref.split('/', 1)[0]
        # Docker Hub limits can affect subsequent hosts even for different tags.
        limit_scope = 'docker-hub' if registry == 'docker.io' else host_scope
        return (scope, ref), (limit_scope, registry)

    def get(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            fetched, value = entry
            if self._clock() - fetched >= self.TTL:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return deepcopy(value)

    def blocked(self, key):
        with self._lock:
            return self._clock() < self._limits.get(key, (0, 0))[1]

    def rate_limit(self, key):
        with self._lock:
            count = min(self._limits.get(key, (0, 0))[0] + 1, 3)
            self._limits[key] = (count, self._clock() + (900, 1800, 3600)[count - 1])

    def put(self, key, value):
        clean = snapshot(value)
        with self._lock:
            self._entries[key] = (self._clock(), clean)
            self._entries.move_to_end(key)
            while len(self._entries) > 1024:
                self._entries.popitem(last=False)
        return deepcopy(clean)
