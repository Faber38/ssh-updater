"""Allowlisted presentation of existing snapshots. No I/O or live diagnostics."""
from datetime import datetime, timezone
import re
import shlex

from . import __version__
from .core.docker_image_updates import REASONS, descriptor, CheckFailure
from .docker_plan import image_identity, project_eligible
from .core.docker_diagnostic_codes import DIAGNOSTICS, allowed_code

MISSING = 'nicht erfasst'
STATES = {'current': 'aktuell', 'update_available': 'Update erkannt',
          'uncheckable': 'Prüfung unvollständig', 'local_build': 'Lokaler Build',
          'digest_pinned': 'Digest-fixiert'}

# These codes prove failure of config resolution as accepted by the checker,
# not necessarily a nonzero Compose exit (e.g. stderr with exit zero).
CONFIG_FAILURE_CODES = frozenset(('COMPOSE_CONFIG_FAILED', 'COMPOSE_CONFIG_INVALID_JSON',
                                  'COMPOSE_CONFIG_STDERR'))
CODE_PHASES = {
    **dict.fromkeys(CONFIG_FAILURE_CODES, 'Compose-Config-Auflösung'),
    'COMPOSE_ENV_EXPECTED_BUT_ABSENT': 'Compose-Kontextprüfung',
    'COMPOSE_CONTEXT_CHANGED': 'Compose-Kontextprüfung',
    'IMAGE_DESCRIPTOR_MISSING': 'Lokale Image-Identitätsprüfung',
}


def mapping(value):
    return value if isinstance(value, dict) else {}


def known(value, choices):
    return value if isinstance(value, str) and value in choices else None


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', value):
        return MISSING
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return MISSING


def version(value, prefix):
    if not isinstance(value, str):
        return MISSING
    match = re.fullmatch(r'(?:' + prefix + r')?v?(\d{1,4}\.\d{1,4}(?:\.\d{1,4})?)(?:, build [0-9a-f]{7,40})?', value)
    return match[1] if match else MISSING


def registry_phase(row, reason):
    if reason == 'rate_limit_backoff':
        return True
    command = row.get('command')
    if not isinstance(command, str):
        return False
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    return (len(args) == 7 and args[:4] == ['docker', 'buildx', 'imagetools', 'inspect']
            and args[5:] == ['--format', '{{json .Manifest}}'])


def diagnostic_details(row, language='de'):
    """Only known codes paired with their primary reason may cross the export boundary."""
    english = language == 'en'
    missing = ('More specific cause was not recorded in the check result.' if english else
               'Genauere Ursache im Prüfergebnis nicht erfasst.')
    state = known(row.get('status'), STATES)
    no_error = row.get('reason') in (None, '') and row.get('diagnostic_code') in (None, '')
    if no_error and state in ('current', 'update_available'):
        return None, 'No error detected.' if english else 'Kein Fehler festgestellt.'
    if no_error and state == 'local_build':
        return None, ('Local build; no registry check performed.' if english else
                      'Lokaler Build; keine Registry-Prüfung durchgeführt.')
    if no_error and state == 'digest_pinned':
        return None, ('Digest-pinned image; no registry tag check performed.' if english else
                      'Digest-fixiertes Image; keine Registry-Tag-Prüfung durchgeführt.')
    code = allowed_code(row.get('diagnostic_code'), row.get('reason')) if state == 'uncheckable' else None
    return (code, DIAGNOSTICS[code][2 if english else 1]) if code else (None, missing)


def report(host, project, *, exported_at=None, language='de'):
    """Never copy names, paths, commands, notes, descriptors or arbitrary strings."""
    host, project = mapping(host), mapping(project)
    check = mapping(project.get('image_updates'))
    raw_rows = check.get('images')
    rows = [mapping(r) for r in raw_rows] if isinstance(raw_rows, list) else []
    details = [diagnostic_details(r, language) for r in rows]
    codes = {code for code, _ in details if code}
    states = [known(r.get('status'), STATES) for r in rows]
    context = check.get('compose_context')
    context = known(context[0], ('EMPTY_CONTEXT', 'PROJECT_DOTENV_CONTEXT')) if isinstance(context, (list, tuple)) and context else None
    identity = check.get('compose_identity')
    resolved = (isinstance(identity, (list, tuple)) and len(identity) == 3
                and all(isinstance(h, str) and re.fullmatch('[0-9a-f]{64}', h) for h in identity[:2]))
    try:
        eligible = project_eligible(project.get('config_files'), tuple(image_identity(r) for r in rows),
                                   tuple(check.get('compose_context', ('EMPTY_CONTEXT', ''))),
                                   tuple(check.get('compose_identity', ())))
    except (TypeError, ValueError, IndexError, AttributeError):
        eligible = False
    now = exported_at or datetime.now(timezone.utc)
    lines = ['SSH Updater – Docker-Diagnose', '', 'ALLGEMEIN',
             f'SSH-Updater-Version: {__version__}',
             f'Prüfergebnis (UTC): {timestamp(check.get("checked_at"))}',
             f'Export (UTC): {now.astimezone(timezone.utc).isoformat()}',
             'Host: Host 1 (anonymisiert)',
             f'Docker-Version: {version(host.get("docker_version"), "Docker version ")}',
             f'Compose-Version: {version(host.get("compose_version"), "Docker Compose version ")}',
             f'Buildx-Version: {MISSING}', '', 'PROJEKT',
             'Projekt: Projekt 1 (anonymisiert; in der Vorschau ausgewählt)',
             'Prüfstatus: ' + ('Prüfung unvollständig' if not states or any(s is None or s == 'uncheckable' for s in states)
                              else ' / '.join(dict.fromkeys(STATES[s] for s in states))),
             'Update erkannt: ' + (MISSING if not states else 'ja' if 'update_available' in states else 'nein (kein Nachweis im Ergebnis)'),
             'Freigabe möglich: ' + ('ja (Vorschau; Live-Preflight weiterhin erforderlich)' if eligible else 'nein'),
             '', 'KONTEXT', f'Compose-Kontext: {context or MISSING}',
             'Standard-.env vorhanden: ' + ('ja' if context == 'PROJECT_DOTENV_CONTEXT' else
                                           'nein' if 'COMPOSE_ENV_EXPECTED_BUT_ABSENT' in codes else MISSING),
             'Kontextprüfung: ' + ('erfolgreich' if resolved else 'fehlgeschlagen' if
                                   any(r.get('reason') == 'context' for r in rows) or
                                   'COMPOSE_CONTEXT_CHANGED' in codes else MISSING),
             'Compose-Config-Auflösung: ' + ('erfolgreich' if resolved else
                                            'fehlgeschlagen' if codes & CONFIG_FAILURE_CODES else MISSING),
             f'Preflight-Bericht: {MISSING} (nicht im Vorschau-Snapshot enthalten)']
    aliases = {'service': {}, 'image': {}}
    def alias(row, field, label):
        value = row.get(field)
        if not isinstance(value, str) or not value:
            return MISSING
        table = aliases[field]
        if value not in table:
            table[value] = len(table) + 1
        return f'{label} {table[value]} (anonymisiert)'
    for index, row in enumerate(rows, 1):
        state = known(row.get('status'), STATES)
        reason = known(row.get('reason'), REASONS)
        code, detail = details[index - 1]
        no_error = (state in ('current', 'update_available', 'local_build', 'digest_pinned')
                    and row.get('reason') in (None, '') and row.get('diagnostic_code') in (None, ''))
        registry = bool(code and code.startswith('REGISTRY_')) or registry_phase(row, reason)
        try:
            descriptor(row.get('local_descriptor'))
            has_descriptor = 'ja'
        except (CheckFailure, TypeError, ValueError):
            has_descriptor = 'nein' if code == 'IMAGE_DESCRIPTOR_MISSING' else MISSING
        compared = state in ('current', 'update_available')
        lines += ['', f'IMAGE / FEHLER {index}',
                  f'Service: {alias(row, "service", "Service")}',
                  f'Image-Referenz: {alias(row, "image", "Image")}',
                  f'Prüfstatus: {STATES.get(state, MISSING)}',
                  'Prüfphase: ' + CODE_PHASES.get(code, 'Registry-Abfrage' if registry else 'Docker-Prüfung (Teilschritt nicht erfasst)'),
                  f'Fehlerklasse: {reason or ("keine" if no_error else MISSING)}',
                  'Beschreibung: ' + (detail if code else REASONS[reason] if reason else detail),
                  f'Diagnosecode: {code or ("keiner" if no_error else MISSING)}',
                  f'Detailhinweis: {detail}',
                  'Image-Identität prüfbar: ' + ('ja' if compared else 'nein' if reason in ('identity', 'local_image_unavailable') else MISSING),
                  f'Lokaler Descriptor vorhanden: {has_descriptor}',
                  'Plattformvergleich: ' + ('erfolgreich' if compared else 'fehlgeschlagen' if reason == 'platform' else MISSING),
                  'Registry-Fehlerklasse: ' + (reason if registry and reason else MISSING)]
    lines += ['', 'DATENSCHUTZ UND GRENZEN',
              'Nur vorhandenes Prüfergebnis; keine neue Verbindung oder Containeränderung.',
              'Namen und Image-Referenzen sind neutral ersetzt; Adressen und Pfade fehlen.',
              'Keine Config-, Environment-, Credential-, Roh-Ausgabe- oder Exception-Inhalte.',
              'Freitext-Hinweise werden durch feste Fehlerbeschreibungen ersetzt.',
              'Nicht erfasst bedeutet unbekannt, nicht automatisch fehlgeschlagen.',
              'Speichern und Weitergabe erfolgen ausschließlich durch den Benutzer.', '']
    return '\n'.join(lines)
