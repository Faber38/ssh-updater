"""Compose input policy. No dotenv parsing and no environment values retained."""
from pathlib import PurePosixPath
import re
import shlex

EMPTY_CONTEXT = 'EMPTY_CONTEXT'
PROJECT_DOTENV_CONTEXT = 'PROJECT_DOTENV_CONTEXT'
EMPTY = (EMPTY_CONTEXT, '')

# Preserve Docker endpoint/config/auth discovery and executable/helper lookup.
# Arbitrary application variables, SSH startup scripts and COMPOSE_* are excluded.
INFRASTRUCTURE = (
    'HOME', 'PATH', 'DOCKER_CONFIG', 'DOCKER_CONTEXT', 'DOCKER_HOST',
    'DOCKER_TLS', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'DOCKER_API_VERSION',
    'DOCKER_CUSTOM_HEADERS', 'DOCKER_DEFAULT_PLATFORM',
    'XDG_RUNTIME_DIR', 'XDG_CONFIG_HOME', 'SSH_AUTH_SOCK',
    'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'ALL_PROXY',
    'http_proxy', 'https_proxy', 'no_proxy', 'all_proxy',
    'SSL_CERT_FILE', 'SSL_CERT_DIR',
)
CONTROLS = (
    'COMPOSE_FILE=', 'COMPOSE_PROJECT_NAME=', 'COMPOSE_PROFILES=',
    'COMPOSE_ENV_FILES=', 'COMPOSE_DISABLE_ENV_FILE=1', 'COMPOSE_PATH_SEPARATOR=:',
    'COMPOSE_CONVERT_WINDOWS_PATHS=0', 'COMPOSE_IGNORE_ORPHANS=0',
    'COMPOSE_REMOVE_ORPHANS=0', 'COMPOSE_PARALLEL_LIMIT=1',
    'COMPOSE_ANSI=never', 'COMPOSE_STATUS_STDOUT=0', 'COMPOSE_PROGRESS=quiet',
    'COMPOSE_MENU=0', 'COMPOSE_EXPERIMENTAL=0', 'COMPOSE_BAKE=false',
    'COMPOSE_COMPATIBILITY=0',
)

# Path checks disclose only a context kind; never read .env contents ourselves.
# Reject symlinks and noncanonical paths for the new context (including parents).
CONTEXT_SCRIPT = '''
if [ ! -e "$1/.env" ] && [ ! -L "$1/.env" ]; then
    printf EMPTY_CONTEXT
    exit 0
fi
[ -f "$1/.env" ] && [ -r "$1/.env" ] && [ ! -L "$1/.env" ] || exit 1
[ "$(realpath -e -- "$1")" = "$1" ] || exit 1
[ "$(realpath -e -- "$2")" = "$2" ] || exit 1
[ "$(realpath -e -- "$1/.env")" = "$1/.env" ] || exit 1
printf PROJECT_DOTENV_CONTEXT
'''


def probe_command(path):
    return ['sh', '-c', CONTEXT_SCRIPT, 'ssh-updater-context',
            str(PurePosixPath(path).parent), path]


def interpolation_allowed(source):
    """Narrow Compose-source admission, deliberately not a YAML/dotenv parser."""
    # Consume escapes before variables, so $${HOME} is literal, not an input.
    tokens = re.compile(r'\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)\}')
    names = [m.group(1) for m in tokens.finditer(source) if m.group(1) is not None]
    return ('$' not in tokens.sub('', source)
            and all(n not in INFRASTRUCTURE and not n.startswith(('COMPOSE_', 'DOCKER_'))
                    for n in names))


def config_command(name, path, context):
    return operation_command(name, path, context, ['config', '--format', 'json', '--no-env-resolution'])


def valid_context(paths, context):
    """Validate the plan's context binding; live canonical checks remain mandatory."""
    if context == EMPTY:
        return True
    if not paths or len(paths) != 1 or not isinstance(paths[0], str):
        return False
    path = PurePosixPath(paths[0])
    return (paths[0].startswith('/') and not paths[0].startswith('//')
            and str(path) == paths[0] and '..' not in path.parts
            and not any(c in paths[0] for c in (',', '\n', '\r'))
            and context == (PROJECT_DOTENV_CONTEXT, str(path.parent / '.env')))


def operation_command(name, path, context, operation):
    """Same ENV-1 environment for config and the two restricted update operations."""
    if context == EMPTY or not valid_context((path,), context):
        raise ValueError('Unsupported Compose context')
    config = ['config', '--format', 'json', '--no-env-resolution']
    pull = ['pull', '--policy', 'always', '--quiet', '--']
    apply = ['up', '-d', '--no-deps', '--pull', 'never', '--no-build', '--']
    if operation != config:
        prefix = pull if operation[:len(pull)] == pull else apply
        services = operation[len(prefix):]
        if (operation[:len(prefix)] != prefix or not services
                or (prefix == pull and len(services) != 1)
                or len(set(services)) != len(services)
                or any(not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', s) for s in services)):
            raise ValueError('Unsupported Compose operation')
    args = ['--project-name', name, '--project-directory', str(PurePosixPath(path).parent),
            '--env-file', context[1], '-f', path, *operation]
    # Keep original arguments in shell positional parameters while constructing
    # the clean environment. Infrastructure assignments precede the executable.
    # Bind absent infrastructure keys too: dotenv must not supply CLI settings.
    script = '\n'.join([
        'set -- ' + shlex.join(args),
        'set -- docker compose "$@"',
        *(f'set -- "{key}=${{{key}-}}" "$@"'
          for key in INFRASTRUCTURE),
        'exec env -i ' + shlex.join(CONTROLS) + ' "$@"',
    ])
    return ['sh', '-c', script, 'ssh-updater-dotenv-' + operation[0]]
