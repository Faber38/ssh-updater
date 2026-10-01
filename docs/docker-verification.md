# Docker Verification – read-only Abschlussverifikation

`core/docker_verification.py` verarbeitet ausschließlich einen erfolgreichen
Apply-Bericht mit `verification_pending`. Der Apply-Bericht bleibt bis zum
vollständigen Abschluss unverändert. Registryfehler können den historischen
Apply-Erfolg nicht rückwirkend ändern. Die GUI startet diesen Schritt über
„Docker prüfen“, ausschließlich nach vollständig erfolgreichem
[Apply](docker-apply.md).

## Lokale Prüfung und Vergleich

Pro vorbereitetem Host/Projekt werden über die bestehende SSH-/Host-Key-Schicht
frisch die Compose-Datei, die aufgelöste Konfiguration und laufende Container
gelesen. Verwendet werden `Checker.resolve_project`, `Checker.image` mit
`local_only=True` und anschließend `Checker.project`. Es gibt keine neue
Digest-Vergleichslogik: Descriptoren, Container-Manifest-Fallback, Plattform und
Multi-Arch werden durch die bestehende Imageprüfung/`compare()` ausgewertet.

Compose-Identität, Servicezuordnung, Container-ID, Image-Referenz, Image-ID,
Descriptor, Plattform, Startzeit und Restart-Zähler müssen zum erfolgreichen
Apply-Bericht passen. Die lokalen Prüfungen werden nach dem Registryzugriff
wiederholt, damit eine zwischenzeitliche Containeränderung keinen Abschluss
begründet. Abweichungen bedeuten: Apply war erfolgreich, aktueller lokaler
Zustand nicht mehr bestätigt. Keine Reparatur und kein erneuter Apply.

Die Verifikation bindet auch Kontextart und kanonischen Standard-`.env`-Pfad aus
dem Apply-Bericht. Die effektive Config wird mit derselben kontrollierten
Environment-Policy erneut aufgelöst und gegen den freigegebenen Hash geprüft,
auch am Ende der lokalen Kontrolle. Abweichende Kontext-/Config-Identitäten
im abschließenden Image-Prüfergebnis dürfen ebenfalls nicht veröffentlicht werden.

Ausschließlich read-only Befehle: `cat -- <Compose-Datei>`,
`docker container ls`, `docker container inspect`, `docker image inspect`,
explizites `docker compose ... config --format json --no-interpolate
--no-env-resolution`, bei Bedarf `docker buildx version` und
`docker buildx imagetools inspect <Referenz> --format '{{json .Manifest}}'`.
Bei `PROJECT_DOTENV_CONTEXT` ersetzt der gebundene lokale `.env`-Pfad `/dev/null`
und `--no-interpolate` entfällt; `--no-env-resolution` bleibt erhalten.
Kein Environment-Dump und keine Config-Persistenz.

## Zulässige Registry-Snapshots

Die normale RegistrySession wird weiterverwendet: 30 Minuten TTL, gleiche
öffentliche/hostbezogene Schlüssel, gleiche 429-Sperren. Insbesondere sind
localhost/private Referenzen weiterhin an den bisherigen Host-/Auth-Scope
gebunden. Es werden keine Cacheeinträge künstlich gelöscht oder Sperren umgangen.

Die TTL allein belegt nicht, dass ein Snapshot zum gepullten Image gehört.
Eine kleine vorgeschaltete Cache-Sicht akzeptiert deshalb einen vorhandenen
frischen Snapshot für die vorbereiteten Images nur, wenn die bestehende
`compare()`-Funktion ihn eindeutig als gleich zum beim Apply bestätigten lokalen
Descriptor einordnet. Ein passender frischer Snapshot ist auch während Backoff
zulässig. Ein alter, abgelaufener oder nicht eindeutig passender Snapshot ist
kein Abschlussbeweis: dann Live-Abfrage, sofern der bestehende Backoff sie
zulässt. Backoff ohne geeigneten Snapshot bleibt ein offenes Prüfergebnis.

Frische Live-Ergebnisse innerhalb desselben Prüflaufs können für identische
Cache-Schlüssel wiederverwendet werden, auch wenn sie einen neuen Stand zeigen.
Andere Projektservices verwenden die normale TTL-Strategie. Auch ein zulässiger
Cache-Treffer ist eine Aussage innerhalb des TTL-Fensters, keine Garantie gegen
eine seit der Abfrage erneut geänderte Registry. Es werden keine neueren Tags
oder Versionssprünge gesucht.

## Ergebnisse und Lebenszyklus

- `verified`: vorbereitete Container lokal bestätigt und gegenüber zulässigem
  Registry-Stand aktuell.
- `update_available`: vorbereitete Container laufen, derselbe Tag zeigt aber
  auf einen anderen Stand; Apply bleibt erfolgreich, neue Vorschau erforderlich.
- `registry_pending`: Apply erfolgreich, Registry-/Vergleichsergebnis offen.
- `local_changed`: Apply war erfolgreich, aktueller lokaler Zustand abweichend
  oder nicht eindeutig bestätigbar.

Einzelne vollständig geprüfte Projekte behalten ihre Ergebnisse auch bei Fehlern
anderer Projekte. Ein erneuter Versuch prüft nur noch offene Projekte. Solche
Ergebnisse dokumentieren den Zeitpunkt ihrer erfolgreichen Prüfung; sie sind
keine zeitlich unbegrenzte Zusicherung des aktuellen Zustands aller Hosts.
HTTP 429, Timeout, Netzwerk- und Authentifizierungsfehler bei der Registry
werden als ausstehende Verifikation behandelt, nicht als Apply-Fehler.
Registryfehler und Abbruch lassen `verification_pending` und den ursprünglichen
Apply-Erfolg bestehen. Der Button „Docker prüfen“ bleibt verfügbar; bei Backoff
wird ohne Registry-Zugriff dessen sicherer Fehlergrund angezeigt.

Der Worker verwendet das bestehende Thread-/Abbruchmodell. Währenddessen sind
konkurrierende Systemaktionen/Vorschauen gesperrt. Die GUI aktualisiert die
normalen Docker-Projekt-Sitzungsdaten und Projektzeilen aus den neuen
Image-Ergebnissen, ohne APT-/Hoststatus oder Datenbank-Prüfwerte zu schreiben.
Sind alle relevanten Projektimages aktuell, erscheint wieder `✓ aktuell`;
ein neuer Registry-Stand ergibt erneut `↑ Image-Update`. Die Projektanzeige
fasst auch nicht angewendete Services zusammen: Deren Fehler oder Updates
können weiterhin sichtbar bleiben, obwohl die vorbereiteten Services bereits
verifiziert sind.

Sind alle vorbereiteten Projekte verifiziert oder weisen einen erneuten
Update-Stand auf, werden Pull-/Apply-Zwischenzustände entfernt. „Docker-Update“
ist zunächst deaktiviert; eine neue Vorschau muss erneut eine Freigabe erzeugen.
Der alte Plan wird niemals wieder aktiviert. Ein Ergebnisbericht darf weiter im
RAM angezeigt werden. Kein Zustand überlebt den Programmneustart.

## Grenzen und Sicherheit

Keine verändernden Docker-Befehle, kein Pull, Up/Down, Restart/Stop/Start,
Build/Push, rm/rmi oder Prune; keine Compose-Dateiänderungen. Keine Persistenz.
Keine Registry-Credentials oder rohe Fehlermeldungen in Dialogen.
Keine atomare Sperre gegen externe Eingriffe nach den read-only Prüfungen.
