# Docker-Compose-Erkennung

Die separate [Read-only-Imageprüfung](docker-image-updates.md) ergänzt nach der
Discovery die Projektdaten. Ihre Registry-Abfragen sind nicht Bestandteil von
`docker_compose.py`; die folgenden drei Befehle beschreiben nur die Discovery.

Die Host-Prüfung `check_updates_for_host()` ruft nach erfolgreichem
SSH-Verbindungsaufbau `docker_compose.discover(conn)` auf. Sie ergänzt ihr
Ergebnis um ein serialisierbares Dictionary unter `docker_compose`, auch wenn
die anschließende Paketprüfung fehlschlägt. Die bestehenden Ergebnisfelder
behalten ihre Bedeutung. Bei fehlgeschlagenem Verbindungsaufbau gibt es keine
Docker-Erkennung und kein zusätzliches Ergebnisfeld.

Die GUI-unabhängige Core-Komponente liefert `DockerComposeDiscovery` mit
Verfügbarkeiten (True/False/None für unbekannt), Versionsausgaben, Projektliste,
Status und Diagnose (Befehl, Exitcode, Hinweis). Sie führt ausschließlich aus:

1. `docker --version`
2. `docker compose version`
3. `docker compose ls --format json`

Nach einem Fehler werden keine weiteren Docker-Befehle ausgeführt. Es gibt
keinen sudo-Aufruf und keinen Fallback auf Compose V1. Die Versionsausgaben
werden unverändert bis auf äußere Leerzeichen gespeichert; eine feste
Versionssyntax wird nicht vorausgesetzt.

| Status | Bedeutung |
| --- | --- |
| `ok` | Erkennung erfolgreich; Projektliste kann leer sein |
| `docker_missing` | Docker-Befehl nicht gefunden |
| `compose_missing` | Compose-Unterbefehl/Plugin nicht vorhanden |
| `permission_denied` | Docker-Zugriff verweigert |
| `daemon_unreachable` | Docker-Daemon/Endpunkt nicht erreichbar |
| `timeout` | Zeitlimit überschritten |
| `invalid_output` | Ungültiges JSON oder unerwartete Projektstruktur |
| `command_error` | Sonstiger Exit-/SSH-/Clientfehler |

Fehlermeldungen werden konservativ anhand bekannter Textmerkmale zugeordnet;
unbekannte oder anderssprachige Fehler bleiben `command_error`. Ein Fehler
beim Auflisten setzt bereits bestätigte Verfügbarkeiten nicht zurück.

`ComposeProject` enthält Name, optionalen Originalstatus, Konfigurationspfade
und gegebenenfalls Containerzahlen je Status sowie deren Summe. Zahlen werden
nur aus vollständig erkannten Zusammenfassungen wie `running(2),exited(1)`
abgeleitet. Unbekannte Statusformate bleiben erhalten, ohne eine Zahl zu
erfinden. Es werden keine einzelnen Container zusätzlich abgefragt.
Kommagetrennte Konfigurationspfade werden aufgeteilt; wegen möglicher Kommata
in Dateinamen bleibt zusätzlich `config_files_raw` erhalten.

Der Parser akzeptiert JSON-Arrays, einzelne Projektobjekte und `null` als leere
Liste. Fehlende optionale Felder sind erlaubt, unbekannte Felder werden
ignoriert. Leere Ausgabe, fehlerhafte Einträge und unerwartete Feldtypen sind
Fehler, keine erfolgreiche leere Bestandsaufnahme.

Laut [Docker-Referenz](https://docs.docker.com/reference/cli/docker/compose/ls/)
listet dieser Befehl standardmäßig laufende Projekte. Vollständig gestoppte
Projekte werden ohne `--all` nicht erfasst. Es erfolgt keine Dateisystemsuche
nach weiteren Compose-Dateien.

Jeder Befehl nutzt `remote_process.capture` mit 10 Sekunden Zeitlimit und
dessen bestehendem Ausgabelimit (4 MiB). Damit entstehen höchstens drei
begrenzte zusätzliche Abfragen pro Host-Prüfung. Docker-Fehler werden als
separates Ergebnis zurückgegeben; Task-Abbrüche propagieren weiterhin.

## Einordnung im Updatezyklus

Discovery → [Imageprüfung](docker-image-updates.md) → lokale Update-Vorschau /
Freigabeplan → [Preflight](docker-preflight.md) → [Pull](docker-image-pull.md)
→ [Apply](docker-apply.md) → [Abschlussverifikation](docker-verification.md).

Discovery liefert Bestandsdaten; Imageprüfung vergleicht Identitäten. Erst die
separaten Pull-/Apply-Komponenten verändern den Imagebestand beziehungsweise
Container. Apply-Erfolg und Registry-Verifikation bleiben getrennte Ergebnisse.
Der vorgesehene Einsatz erfolgt auf eigenen oder vertrauenswürdigen Systemen
in einem kontrollierten sicheren LAN. Das ersetzt weder SSH-/Host-Key-Prüfung
noch angemessene Rechte und anwendungsspezifische Sicherungen.

## Anzeige in der Hostliste

Die Spalte `Docker` steht zwischen `Status` und `Letzte Prüfung`. Sie wertet
nur das vorhandene `docker_compose`-Ergebnis aus; Paketstatus und Updateanzahl
bleiben unabhängig davon. Es gibt keine zusätzlichen Remote-Befehle und keine
Datenbankpersistenz der Docker-Daten.

| Ergebnis | Anzeige |
| --- | --- |
| Noch kein Ergebnis | `Nicht geprüft` |
| `docker_missing` | `—` |
| `ok` (auch ohne Projekte) | `🐳 Docker <Version>` |
| `compose_missing` | `🐳 Docker <Version> – Compose fehlt` |
| `permission_denied` | `⚠ Keine Berechtigung` |
| `daemon_unreachable`, `timeout`, `invalid_output`, `command_error` | `⚠ Docker-Fehler` |

Für die Anzeige wird die Versionsnummer aus der Docker-Versionsausgabe
extrahiert, einschließlich etwaiger Vorab-/Distributionssuffixe. Ein optionales
`v` und der `, build ...`-Teil werden nicht angezeigt. Auch eine reine
Versionsnummer wird akzeptiert. Bei unbekanntem Format erscheint
`🐳 Docker (Version unbekannt)` (gegebenenfalls mit ` – Compose fehlt`). Die
ursprüngliche Versionsausgabe im Erkennungsergebnis bleibt unverändert.

Das Hauptfenster hält Docker-Ergebnisse nur im Arbeitsspeicher pro Host-ID.
Beim Neuladen der Hostliste (auch nach dem Konfigurationsdialog) bleiben sie
erhalten, sofern IP, Benutzer, Port, Authentifizierungsmethode, Schlüsselpfad
und verschlüsseltes Passwort unverändert sind. Gelöschte Hosts werden aus dem
Sitzungsspeicher entfernt; neue Hosts beginnen mit `Nicht geprüft`.
Ein neues Hauptfenster beziehungsweise ein Programmneustart beginnt ebenfalls
ohne Docker-Ergebnisse. Es werden keine Docker-Daten in Einstellungen oder
Datenbank geschrieben.

Vor jeder neuen Prüfung mit ausgewählten Hosts werden Sitzungsspeicher und
alle Docker-Zellen zurückgesetzt, auch für die diesmal nicht ausgewählten
Hosts. Ein Prüfergebnis ohne Docker-Daten verwirft ebenfalls den vorherigen
Status und zeigt `Nicht geprüft`, beispielsweise nach einem Verbindungsfehler
oder Abbruch. Neue Ergebnisse ersetzen den bisherigen Status des Hosts.

### Compose-Projekte als untergeordnete Zeilen

Erkannte Projekte stehen eingerückt unter ihrem Host im QTreeView. Hostgruppen
werden gemeinsam sortiert; Projektzeilen bleiben beim Parent. Die Kinder zeigen
Projektname, Compose-Status und Image-Status, ohne IP, Benutzer oder Prüfzeit des
Hosts zu wiederholen. Tabellenlinien und dezente Hintergründe pro Hostgruppe
machen die Zuordnung sichtbar.

Hostcheckboxen wählen Systemaktionen aus, Projektcheckboxen ausschließlich
Docker-/Compose-Aktionen. Beide Auswahlen sind unabhängig und ohne automatische
Vorauswahl. Der Expander blendet Kinder nur ein oder aus; Auswahl und
Sitzungsergebnisse bleiben erhalten. Weder Aufklappen noch Auswählen führt
Remote-Abfragen aus. Neue Ergebnisse aktualisieren die Kinder; verschwundene
Projekte und geänderte Host-Verbindungskontexte verwerfen deren Auswahl.

## Schreibgeschützte Details

Docker-Zellen mit erkanntem Docker oder vorhandenen Fehlerdiagnosen sind
unterstrichen. Beim Überfahren erscheint ein Handcursor und der Tooltip
`Docker-Details anzeigen` beziehungsweise `Docker-Fehlerdetails anzeigen`.
Ein einfacher Linksklick öffnet `Docker – <Hostname>`. `Nicht geprüft` und
`—` sind nicht interaktiv; andere Tabellenzellen behalten ihr Verhalten.

Der Dialog zeigt ausschließlich eine Kopie des letzten Sitzungsergebnisses:
Engine-/Compose-Version, einen verständlichen Status und eine schreibgeschützte
Tabelle mit Projektname, Originalstatus, Containeranzahl und Compose-Pfaden.
Unbekannte Containerzahlen erscheinen als `—`, eine bekannte Null als `0`.
Der ursprüngliche Pfadstring wird bevorzugt angezeigt, um mehrdeutige Kommata
in Dateinamen zu erhalten.

Eine erfolgreiche leere Projektabfrage zeigt `Keine Compose-Projekte gefunden`.
Bei Fehlern steht stattdessen `Projektliste nicht verfügbar`; Statuscode,
Befehl, Exitcode und Originalmeldung der Discovery werden als auswählbarer Klartext angezeigt.
Der Dialog führt keine SSH-Verbindungen oder Docker-Abfragen aus und bietet
ausschließlich eine Schließen-Schaltfläche. Beim nächsten Öffnen wird das
dann aktuelle Sitzungsergebnis verwendet.

Zusätzlich enthält die Projekttabelle jetzt `Image-Status`; die Auswahl zeigt
die Service-/Image-Ergebnisse der letzten normalen Prüfung. Details und
bewusste Compose-Grenzen stehen in der verlinkten Imageprüfungs-Dokumentation.

Integration ausschließlich in die Host-Prüfung: keine zusätzlichen Abfragen
bei Upgrade, Simulation, Autoremove, Reboot oder Host-Key-Inspektion.
Verwaltete Verbindungen verwenden direkte Ziele; ProxyJump wird nicht unterstützt. SSH-Verbindungs- und Sicherheitsrichtlinien,
DB-/Konfigurationsschema und Anwendungsversion sind unverändert.
Discovery und Detailansicht führen keine Docker-Updates, Neustarts oder
Löschaktionen aus. Der separate Updatezyklus verwendet ihre Sitzungsergebnisse
als Ausgangspunkt und prüft die Identitäten vor Änderungen erneut live.
