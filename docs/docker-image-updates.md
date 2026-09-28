# Docker-Image-Prüfung

## Bedeutung

Ein Update bedeutet eine nachgewiesene Änderung **desselben konfigurierten
Tags** auf einer eindeutig vergleichbaren Manifest-Ebene. `nginx:1.28-alpine`
wird nur mit `nginx:1.28-alpine` verglichen. SSH Updater sucht keine neueren
Tags und schlägt keinen Wechsel zu `1.29-alpine` vor. Tags können auch
zurückgesetzt werden: „Update“ behauptet keine chronologische Verbesserung.

Die Prüfung liest lokale Metadaten und Registry-Manifeste. Sie lädt keine
Image-Layer in den Docker-Image-Speicher, verändert keine Images, Container,
Compose-Dateien oder Projekte und führt keinen Login aus. Die GUI bietet einen
separaten Updatezyklus mit Vorschau, [Preflight](docker-preflight.md),
[Pull](docker-image-pull.md), [Apply](docker-apply.md) und
[Abschlussverifikation](docker-verification.md). Die Imageprüfung selbst führt
keine dieser verändernden Aktionen aus. Registry-Lesezugriffe können Rate Limits
unterliegen.

## Integration und Lebensdauer

`core/docker_image_updates.py` ist GUI-unabhängig. `check_projects(conn,
projects)` verwendet dieselbe bereits authentifizierte SSH-Verbindung wie die
Docker-Discovery. `ssh_client.check_updates_for_host()` ruft die Funktion nach
erfolgreicher Discovery vor der unveränderten Linux-Paketprüfung auf. Die
gesamte Operation läuft im bestehenden `_CheckWorker`, nicht im GUI-Thread.

Jedes Discovery-Projekt erhält `image_updates` mit UTC-Prüfzeit, Zusammenfassung
und einer Liste strukturierter `ImageCheck`-Ergebnisse. Diese enthalten Service,
Container-ID/-Name, konfigurierte Image-Referenz, tatsächlich verwendete
Image-ID, OS/Architektur/Variante, Descriptor und RepoDigests sowie
Vergleichsebene, Registry-Descriptor, Status und gegebenenfalls Fehlergrund.
Vollständige Container-/Image-Konfigurationen werden nicht an die GUI gegeben.

Die bestehende GUI hält diese verschachtelten Daten nur in der Sitzung.
Neuladen unveränderter Hosts erhält sie; neue Prüfung, gelöschte Hosts oder
geänderte Verbindungs-/Authentifizierungsdaten verwerfen sie. Kein DB-Schema,
keine zusätzlichen DB-Schreibvorgänge, keine QSettings-Persistenz.

## Ausschließlich verwendete zusätzliche Befehle

Alle variablen Argumente werden mit `shlex.join()` für den Remote-Aufruf
gequotet. Image-Referenzen und Container-/Image-IDs werden zusätzlich validiert.

1. `cat -- <absoluter Compose-Pfad>`: konservative Vorprüfung der Quelldatei;
   am Projektende erneut gelesen, um zwischenzeitliche Änderungen zu erkennen.
2. `docker container ls --quiet --no-trunc --filter label=com.docker.compose.project=<Projekt>`:
   ausschließlich IDs laufender Container, keine Auswertung von Tabellen.
3. `docker container inspect <IDs...>`: JSON, Service-/Projektlabels,
   tatsächliche Image-ID und ursprüngliche Referenz.
4. `env COMPOSE_PROFILES= COMPOSE_ENV_FILES= COMPOSE_DISABLE_ENV_FILE=1 docker compose --project-name <Projekt> --project-directory <Verzeichnis> --env-file /dev/null -f <Datei> config --format json --no-interpolate --no-env-resolution`:
   Compose selbst erzeugt die strukturierte Service-Konfiguration.
5. `docker image inspect <tatsächlich verwendete Image-ID>`: JSON des laufenden
   Images, ausdrücklich nicht nur des derzeit lokalen Tags.
6. `docker buildx version`: einmalig und nur bei notwendiger Registry-Prüfung.
7. `docker buildx imagetools inspect <kanonische Referenz> --format '{{json .Manifest}}'`:
   strukturierte Registry-Metadaten; kein Layer-Pull.

Keine Befehle zum Pull, Build, Erstellen, Löschen, Starten, Stoppen oder
Neustarten von Images/Containern/Stacks werden aufgerufen.

## Bewusst enger Compose-Kontext

Die Auflösung unterstützt genau eine absolute, eindeutig angegebene lokale
Compose-Datei. Der Discovery-Pfad und die Containerlabels müssen übereinstimmen.
Das gespeicherte Arbeitsverzeichnis muss dem Elternverzeichnis der Datei
entsprechen. Beim Label `com.docker.compose.project.environment_file` sind nur
ein fehlendes Label, ein leerer Wert oder exakt `/dev/null` zulässig. Letzteres
entspricht dem von SSH Updater bewusst verwendeten `--env-file /dev/null` und
wird auch bei selbst neu erstellten Containern akzeptiert. Andere Pfade, `.env`,
mehrere oder kombinierte Angaben werden weiterhin abgelehnt; es gibt keine
allgemeine Unterstützung zusätzlicher Environment-Dateien.
Projekt, Service und laufender Zustand werden anhand der Containerdaten geprüft.

Vor `compose config` wird eine absichtlich konservative Text-Schranke angewandt:
Dateien mit `$`, Backslash, `!`, `&`, `*` oder `%` sowie mit `include`, `extends`,
`profiles`, `env_file`, `provider`, `models`, `pre_start`, `post_start` oder
`pre_stop` werden abgelehnt. Das gilt auch bei Treffern in Kommentaren oder
gewöhnlichen Werten. Diese Schranke ist **kein YAML-Parser** und ersetzt nicht
Compose; sie verweigert komplexe/indirekte Kontexte, bevor Compose entfernte
Quellen auflösen könnte. Sie kann bewusst sichere, aber noch nicht unterstützte
Dateien ausschließen. Die endgültige Interpretation erfolgt durch Compose.

Interpolation und Service-Environment-Auflösung bleiben ausgeschaltet; die
automatische `.env`-Übernahme wird deaktiviert. Deshalb wird niemals anhand
einer zufälligen aktuellen SSH-Shell-Umgebung geraten. Ein einfacher statischer
Service mit `image: nginx:1.28-alpine` ist unterstützt. Mehrere statische Services
innerhalb derselben Datei sind unterstützt, mehrere Dateien/Overrides nicht.

Die konfigurierte Referenz muss nach Normalisierung mit der bei der
Containererstellung verwendeten Referenz übereinstimmen. Abweichungen sind
„Prüfung nicht möglich“. Konfigurierte Services ohne laufenden Container
verhindern eine uneingeschränkte „aktuell“-Zusammenfassung. One-off-Container
werden nicht unterstützt.

## Vergleichsregeln

Es werden nur vollständige SHA256-Descriptoren mit ausdrücklich unterstütztem
`mediaType` verglichen. Die lokale Inspektion muss zur angefragten laufenden
Image-ID gehören. `.Id` oder `RepoDigests` allein sind kein Beleg für eine
bestimmte Manifest-Ebene und werden nie ersatzweise als Index-Digest verwendet.

Unterstützt:

- OCI-Index gegen OCI-Index: identischer Typ, gleicher Digest → `current`,
  anderer Digest → `update_available`.
- Docker-v2-Manifest-List gegen denselben Typ: dieselbe Regel.
- Lokaler OCI-Plattform-Manifest-Descriptor gegen den eindeutig ausgewählten
  OCI-Manifest-Descriptor eines Registry-Index.
- Entsprechender Docker-v2-Plattform-Manifest-Vergleich innerhalb einer Liste.

Die Plattformauswahl nutzt tatsächliches OS/Architektur/Variante. Eine explizite
Compose-Plattform muss dazu passen. Varianten werden nicht geraten.
`unknown/unknown` und als Attestation gekennzeichnete Einträge werden nicht als
passendes Plattform-Image behandelt. Fehlende oder mehrere passende Einträge
sind nicht prüfbar. Identische SHA256-Werte bei verschiedenen Medientypen führen
nicht zur Gleichsetzung.

Ein direkter Index-Vergleich bewertet den gesamten Index. Deshalb kann auch eine Änderung einer anderen Architektur oder
Index-Metadaten einen Unterschied ergeben. `comparison_level = index` hält
diese Semantik fest; ein ausgewähltes Plattform-Manifest verwendet
`comparison_level = platform_manifest`.

Bewusst nicht unterstützt: ältere Stores ohne geeigneten Descriptor,
Config-ID-Fallbacks, Schema-1/exotische Medientypen, Umwandlung zwischen
Docker-/OCI-Medientypen sowie direkte entfernte Einzelmanifeste ohne hier
eindeutig belegte Plattform. Ergebnis: `uncheckable`, niemals geraten „aktuell“.

### Enger Fallback bei fehlendem lokalen Image

`docker image inspect <Container.Image>` bleibt der bevorzugte Weg. Nur wenn
der Daemon ausdrücklich `No such image:` meldet, wird der schon gelesene
`ImageManifestDescriptor` des Containers als Ersatz geprüft. Timeouts,
Berechtigungsfehler, ungültiges JSON und andere Fehler werden nicht verdeckt.

Der Ersatz muss ein OCI- oder Docker-v2-**Plattform-Manifest** mit vollständigem
SHA256-Digest und eindeutigen `platform.os`/`platform.architecture`-Werten sein.
Eine vorhandene Variante muss gültig sein; `unknown`, fehlende Werte,
Index-/Listen-Descriptoren sowie Widersprüche zu Container-OS oder explizitem
Compose-`platform` werden abgelehnt. Der bestehende Vergleich wählt dann das
passende Registry-Plattform-Manifest. Ein Vergleich dieses Ersatzes mit dem
obersten Registry-Index-Digest findet nicht statt.

`identity_source` dokumentiert `image_inspect` oder
`container_manifest_descriptor`. Ersatzdaten werden nicht im Image-ID-Cache
gespeichert, da sie ausschließlich zu diesem Container gehören.
Ist kein belastbarer Ersatz vorhanden, lautet der strukturierte Fehlergrund
`local_image_unavailable`: „Lokales Image über Container-Image-ID nicht
inspizierbar; kein eindeutig gültiger Container-Manifest-Descriptor als Ersatz
verfügbar.“ Befehl und Exitcode bleiben erhalten, rohe Fehlermeldungen nicht.

Digest-Referenzen werden als `digest_pinned` gekennzeichnet und nicht nach
anderen Digests durchsucht. Reine lokale Builds ohne `image` werden als
`local_build` gekennzeichnet. `build` plus `image` ist mangels eindeutiger
Registry-Herkunft nicht prüfbar. Es wird nichts gebaut.

## Grenzen und Fehler

Sequenzielle Verarbeitung, keine zusätzliche Parallelisierung. Pro Befehl
20 Sekunden, insgesamt 180 Sekunden pro Host-Imageprüfung, höchstens 32 Projekte
und 64 Container/Services je Projekt. Der bestehende SSH-Capture begrenzt die
Ausgabe auf 4 MiB. Lokale Images werden nur innerhalb eines Host-Prüflaufs
pro Image-ID zwischengespeichert. Registry-Fehler werden innerhalb dieses
Prüflaufs pro Referenz wiederverwendet; erfolgreiche Metadaten verwenden den
nachfolgend beschriebenen Sitzungscache.

Fehlergründe unterscheiden fehlendes Buildx, Registry-Authentifizierung,
Tag/Image nicht gefunden, Rate Limit, Netzwerk/TLS, Timeout, ungültige Ausgabe,
sonstige Befehlsfehler sowie Kontext-/Identitäts-/Plattformprobleme. Die
Textklassifikation bekannter Registry-Fehler ist konservativ; unbekannte
Meldungen werden als sonstiger Fehler behandelt.

Rohe Registry-/Credential-Helper-Fehler werden nicht in Session oder GUI
übernommen, da sie Geheimnisse enthalten könnten. Angezeigt werden feste
verständliche Gründe, geprüfter Befehl und Exitcode im strukturierten Ergebnis.
Bestehende Registry-Zugänge des SSH-Benutzers werden von Docker/Buildx verwendet;
SSH Updater speichert keine Registry-Zugangsdaten und versucht keinen Login.
Fehlende/nichtinteraktiv unbenutzbare Credential Helper sind nicht prüfbar.

Docker-Imagefehler ändern den Linux-Paketstatus nicht. Selbst unerwartete
Fehler dieser optionalen Komponente erhalten ein Docker-spezifisches Ergebnis.
Ein Benutzerabbruch propagiert weiterhin an den bestehenden Worker.

## GUI

Die Projekttabelle enthält zusätzlich `Image-Status`. Die Auswahl eines
Projekts zeigt Service/Container, Image-Referenz, Plattform, Status und Hinweis
in einer zweiten schreibgeschützten Tabelle. Öffnen/Wechseln der Auswahl löst
keine SSH-/Registry-Abfrage aus; nur die letzte normale Hostprüfung wird gezeigt.

Zusammenfassungen zählen unterschiedliche Image-/Plattform-Paare mit Updates,
nicht deren Replikate. Updates plus Fehler bleiben gleichzeitig sichtbar.
Ungeprüfte relevante Services verhindern `✓ aktuell`. Lokale Builds und
Digest-Pinning bleiben in gemischten Projekten ausdrücklich gekennzeichnet.

## Quellen

- [Compose config](https://docs.docker.com/reference/cli/docker/compose/config/)
- [Buildx imagetools inspect](https://docs.docker.com/reference/cli/docker/buildx/imagetools/inspect/)
- [OCI Image Index](https://github.com/opencontainers/image-spec/blob/main/image-index.md)

## Registry-Sitzungscache (ausschließlich RAM)

`core/registry_session.py` enthält die GUI-unabhängige `RegistrySession`.
Der Anwendungskontext hält eine Instanz und reicht sie an alle normalen
Prüf-Worker weiter. Neue Checker behalten ihre eigenen lokalen Daten.
Programmende verwirft Cache und Backoff vollständig; ein neuer Kontext ist leer.
Es gibt keine Cache-Datei, SQLite-Erweiterung, DB-Schreibzugriffe oder
persistenten Digests beziehungsweise Backoff.

Erfolgreiche, validierte Registry-Descriptoren und Plattform-Manifeste gelten
30 Minuten ab erfolgreicher Abfrage (monotone Uhr). Ein Treffer verlängert
weder TTL noch Lebensdauer. Die Plattformauswahl erfolgt anschließend lokal;
dieselbe Index-Antwort kann mehrere Plattformen bedienen. Der Cache hält nur
Referenz/Bereich, Vergleichsmetadaten und Abfragezeit, maximal 1024 Snapshots.
Freitext, URLs, fremde Annotationen, Credentials und Rohantworten werden nicht
übernommen. Ausgabe und Speicherung verwenden unabhängige Kopien.

Nur die ausdrücklich öffentlichen Docker-Hub-Repositories `library/nginx`,
`library/httpd` und `library/registry` werden hostübergreifend geteilt.
Die bestehende Referenznormalisierung vereinheitlicht Kurzformen. Alle anderen
Repos, privaten/unbekannten Registries und `localhost:5000` bleiben an einen
undurchsichtigen Fingerabdruck des Host-Verbindungskontexts gebunden.
Verbindungs-/Authentifizierungsänderungen erzeugen einen anderen Bereich.
Änderungen an Registry-Credentials direkt auf dem Zielhost sind nicht automatisch
erkennbar; ein Cache-Treffer ist kein Nachweis einer aktuellen Zugriffsberechtigung.

HTTP 429 setzt im RAM eine Sperre von zunächst 15, dann 30 und maximal
60 Minuten. Die Eskalationsstufe bleibt innerhalb der Sitzung erhalten.
Docker-Hub-Sperren gelten registryweit über Hosts hinweg; andere Registries
bleiben hostgebunden. Es gibt keine automatischen Wiederholungen. Frische
zulässige Cache-Einträge dürfen während der Sperre verwendet werden, abgelaufene
niemals. Ohne frischen Eintrag lautet das Ergebnis `Prüfung nicht möglich`
mit Rate-Limit-Hinweis. Andere Fehler erhalten keine zusätzliche sitzungsweite
Sperre und können keine abgelaufenen Antworten wieder gültig machen.

Bei jeder normalen Hostprüfung werden laufende Container, Compose-Konfiguration,
lokale Image-Identität und Plattform neu ermittelt und `compare()` neu ausgeführt.
Ein angezeigtes Ergebnis bezieht sich dabei auf einen höchstens 30 Minuten alten
Registry-Stand, nicht zwingend auf den gegenwärtigen Tag-Stand. Es werden keine
fertigen Vergleichsergebnisse im Registry-Cache gespeichert. Die bestehende
GUI-Ergebnisanzeige bleibt eine Momentaufnahme der letzten Hostprüfung.
