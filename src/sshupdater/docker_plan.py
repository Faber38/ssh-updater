"""Immutable, RAM-only GUI approval identity. No execution or remote access."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ProjectSnapshot:
    host_id: int
    connection: tuple
    revision: int
    name: str
    paths: tuple
    raw_paths: str | None
    status: str
    checked_at: str
    images: tuple
    compose_identity: tuple = ()


@dataclass(frozen=True)
class UpdatePlan:
    selection: tuple[ProjectSnapshot, ...]
    candidates: tuple[ProjectSnapshot, ...]


def image_identity(image):
    def descriptor(value):
        value = value or {}
        return (value.get('mediaType'), value.get('digest'))
    return (image.get('service'), image.get('container_id'), image.get('container_name'),
            image.get('image'), image.get('platform'), image.get('status'),
            image.get('image_id'), image.get('comparison_level'),
            descriptor(image.get('local_descriptor')), descriptor(image.get('remote_descriptor')))


def selection(hosts):
    projects = []
    for host in hosts:
        for project in host['projects']:
            check = project.get('image_updates', {})
            projects.append(ProjectSnapshot(
                host['host_id'], host['connection_identity'], host['result_revision'], project['name'],
                tuple(project.get('config_files') or []), project.get('config_files_raw'), project.get('status', ''),
                check.get('checked_at', ''),
                tuple(sorted((image_identity(i) for i in check.get('images', [])), key=repr)),
                tuple(check.get('compose_identity', ()))))
    return tuple(sorted(projects, key=lambda p: (p.host_id, p.name)))


def proven(image):
    service, cid, name, ref, platform, status, _, level, local, remote = image
    index_types = ('application/vnd.oci.image.index.v1+json',
                   'application/vnd.docker.distribution.manifest.list.v2+json')
    manifest_types = ('application/vnd.oci.image.manifest.v1+json',
                      'application/vnd.docker.distribution.manifest.v2+json')
    return bool(status == 'update_available' and service and (cid or name) and ref
                and platform and 'unknown' not in platform.split('/')
                and local[0] == remote[0]
                and ((level == 'index' and local[0] in index_types)
                     or (level == 'platform_manifest' and local[0] in manifest_types))
                and all(isinstance(d, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', d)
                        for d in (local[1], remote[1])) and local[1] != remote[1])



def project_eligible(paths, images):
    """Shared conservative project approval rule over snapshot identities."""
    return bool(paths and images
                and all(i[5] == 'current' or proven(i) for i in images)
                and any(proven(i) for i in images))


def build_plan(hosts):
    selected = selection(hosts)
    candidates = []
    for project in selected:
        # Phase 3 conservatively excludes projects with unresolved/build/pinned images.
        if project_eligible(project.paths, project.images):
            candidates.append(ProjectSnapshot(
                project.host_id, project.connection, project.revision, project.name,
                project.paths, project.raw_paths, project.status, project.checked_at,
                tuple(i for i in project.images if proven(i)), project.compose_identity))
    return UpdatePlan(selected, tuple(candidates))
