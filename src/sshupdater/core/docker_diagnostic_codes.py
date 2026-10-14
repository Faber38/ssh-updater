"""Fixed diagnostic vocabulary only; never attach values or affect admission."""

# code -> (existing primary reason, German description, English description)
DIAGNOSTICS = {
    'COMPOSE_FILE_BINDING_INVALID': ('context', 'Compose-Dateizuordnung ist nicht eindeutig zulässig.', 'Compose file binding is not unambiguously supported.'),
    'COMPOSE_SOURCE_FILTER': ('context', 'Der Compose-Quelltext wurde vom konservativen Textfilter abgelehnt; dies beweist keinen YAML-Syntaxfehler.', 'The conservative source filter rejected the Compose source; this does not prove a YAML syntax error.'),
    'COMPOSE_INTERPOLATION_REJECTED': ('context', 'Die Interpolationsprüfung hat den Quelltext abgelehnt.', 'The source did not pass interpolation admission.'),
    'COMPOSE_CONTAINER_BINDING_INVALID': ('context', 'Containerzustand oder Compose-Labelbindung stimmt nicht mit dem erwarteten Projekt überein.', 'Container state or Compose label binding does not match the expected project.'),
    'COMPOSE_SERVICE_LABEL_MISSING': ('context', 'Das erforderliche Compose-Service-Label fehlt oder ist leer.', 'The required Compose service label is absent or empty.'),
    'COMPOSE_ENV_LABEL_CONFLICT': ('context', 'Die Environment-Labels widersprechen sich.', 'The environment labels conflict.'),
    'COMPOSE_ENV_PROBE_FAILED': ('context', 'Die Kontextprobe ist fehlgeschlagen oder lieferte keine unterstützte Kontextkennung; die Einzelursache ist nicht erfasst.', 'The context probe failed or returned an unsupported context identifier; the specific cause was not recorded.'),
    'COMPOSE_ENV_EXPECTED_BUT_ABSENT': ('context', 'Das Containerlabel erwartet die Standard-.env, die Kontextprobe meldet jedoch einen leeren Kontext.', 'The container label requires the standard .env, but the context probe reports an empty context.'),
    'COMPOSE_EMPTY_CONTEXT_DOLLAR_REJECTED': ('context', 'Dollarzeichen sind im Compose-Quelltext bei leerem Kontext nicht zugelassen.', 'Dollar signs in Compose source are not allowed with an empty context.'),
    'COMPOSE_CONFIG_FAILED': (None, 'Die bestehende Compose-Config-Abfrage ist fehlgeschlagen.', 'The existing Compose config query failed.'),
    'COMPOSE_CONFIG_INVALID_JSON': ('invalid_output', 'Die Compose-Config-Ausgabe konnte nicht als JSON gelesen werden.', 'The Compose config output could not be parsed as JSON.'),
    'COMPOSE_CONFIG_STDERR': ('context', 'Compose hat bei der Standard-.env-Auflösung trotz erfolgreichem Exit eine stderr-Meldung geliefert.', 'Compose emitted stderr during standard .env resolution despite a successful exit.'),
    'COMPOSE_SERVICE_MAPPING_INVALID': ('context', 'Service-Struktur oder Zuordnung der Container zu den Config-Services ist ungültig.', 'The service structure or container-to-config service mapping is invalid.'),
    'COMPOSE_SERVICE_POLICY_REJECTED': ('context', 'Service-Struktur, Profile oder Service-env_file wurden von der bestehenden Prüfung abgelehnt.', 'Service structure, profiles or service env_file were rejected by the existing check.'),
    'COMPOSE_ENVIRONMENT_REJECTED': ('context', 'Die Service-Environment-Struktur enthält unzulässige oder unaufgelöste Einträge.', 'The service environment structure contains unsupported or unresolved entries.'),
    'IMAGE_REFERENCE_INVALID': ('context', 'Eine Image-Referenz hat die Referenzprüfung nicht bestanden.', 'An image reference failed reference validation.'),
    'IMAGE_REFERENCE_DRIFT': ('drift', 'Compose-Referenz und Container-Referenz unterscheiden sich nach Normalisierung.', 'Normalized Compose and container image references differ.'),
    'IMAGE_ID_INVALID': ('identity', 'Die Container-Image-ID entspricht nicht dem verlangten SHA256-Format.', 'The container image ID does not match the required SHA256 format.'),
    'IMAGE_ID_MISMATCH': ('identity', 'Die inspizierte Image-ID stimmt nicht mit der Container-Image-ID überein.', 'The inspected image ID does not match the container image ID.'),
    'IMAGE_DESCRIPTOR_MISSING': ('identity', 'Der für die Identitätsprüfung benötigte Descriptor fehlt.', 'The descriptor required for identity validation is absent.'),
    'IMAGE_DESCRIPTOR_INVALID': ('identity', 'Descriptor-Struktur, Medientyp oder Digest ist ungültig oder nicht unterstützt.', 'Descriptor structure, media type or digest is invalid or unsupported.'),
    'IMAGE_PLATFORM_INVALID': ('platform', 'Die vorhandenen Plattformdaten sind unvollständig oder ungültig.', 'The available platform data is incomplete or invalid.'),
    'IMAGE_PLATFORM_MISMATCH': ('platform', 'Die Plattformbindung ist widersprüchlich oder die explizite Plattformvorgabe ist ungültig.', 'Platform bindings conflict or the explicit platform constraint is invalid.'),
    'IMAGE_PLATFORM_NOT_FOUND': ('platform', 'Kein unterstützter Plattformtreffer im Remote-Index vorhanden.', 'No supported platform match was found in the remote index.'),
    'IMAGE_PLATFORM_AMBIGUOUS': ('platform', 'Mehrere Plattformtreffer verhindern eine eindeutige Zuordnung.', 'Multiple platform matches prevent unambiguous selection.'),
    'IMAGE_DESCRIPTOR_TYPE_MISMATCH': ('identity', 'Die zu vergleichenden Descriptor-Medientypen unterscheiden sich.', 'The descriptor media types being compared differ.'),
    'IMAGE_COMPARISON_UNSUPPORTED': ('identity', 'Diese Kombination eigenständiger Manifest-Descriptoren wird nicht verglichen.', 'This combination of standalone manifest descriptors is not supported for comparison.'),
    'REGISTRY_AUTH_FAILED': ('auth', 'Die Registry-Abfrage wurde als Authentifizierungs- oder Berechtigungsfehler klassifiziert.', 'The registry query was classified as an authentication or permission failure.'),
    'REGISTRY_RATE_LIMIT': ('rate_limit', 'Die Registry-Abfrage wurde als Rate-Limit-Fehler klassifiziert.', 'The registry query was classified as rate limited.'),
    'REGISTRY_RATE_LIMIT_BACKOFF': ('rate_limit_backoff', 'Eine aktive Rate-Limit-Sperre verhindert die erneute Registry-Abfrage.', 'An active rate-limit backoff prevents another registry query.'),
    'REGISTRY_NETWORK_FAILED': ('network', 'Die Registry-Abfrage wurde als Netzwerk- oder TLS-Fehler klassifiziert.', 'The registry query was classified as a network or TLS failure.'),
    'REGISTRY_TIMEOUT': ('timeout', 'Die Registry-Abfrage hat ihr Zeitlimit überschritten.', 'The registry query exceeded its time limit.'),
    'REGISTRY_METADATA_INVALID': (None, 'Die Registry-Metadaten haben die JSON-, Descriptor- oder Strukturprüfung nicht bestanden.', 'Registry metadata failed JSON, descriptor or structure validation.'),
    'COMPOSE_SOURCE_CHANGED': ('context', 'Der Compose-Quelltext hat sich während der Prüfung geändert.', 'The Compose source changed during the check.'),
    'COMPOSE_CONTEXT_CHANGED': ('config', 'Die abschließende Kontextprobe bestätigt den Standard-.env-Kontext nicht mehr.', 'The final context probe no longer confirms the standard .env context.'),
    'COMPOSE_CONFIG_CHANGED': ('config', 'Der Fingerprint der wirksamen Compose-Konfiguration hat sich während der Prüfung geändert.', 'The effective Compose configuration fingerprint changed during the check.'),
}

# Codes covering more than one existing primary reason have an explicit set too.
MULTI_REASONS = {
    'COMPOSE_CONFIG_FAILED': frozenset(('timeout', 'command_error', 'local_image_unavailable',
                                      'rate_limit', 'auth', 'not_found', 'network')),
    'REGISTRY_METADATA_INVALID': frozenset(('identity', 'invalid_output')),
}


def allowed_code(code, reason):
    if not isinstance(code, str) or code not in DIAGNOSTICS or not isinstance(reason, str):
        return None
    expected = DIAGNOSTICS[code][0]
    return code if (reason == expected or reason in MULTI_REASONS.get(code, ())) else None
