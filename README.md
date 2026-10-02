<p align="center">
  <img src="src/sshupdater/assets/icon.png" alt="SSH Updater Icon" width="120"/>
</p>

# SSH Updater

SSH Updater ist ein Desktop-Werkzeug zum Verwalten und Aktualisieren eigener oder
vertrauenswürdiger Linux-Systeme über SSH innerhalb eines kontrollierten, sicheren
LANs. Es verbindet eine Mehrhostübersicht, Paketprüfung und Systemupdates,
SSH-/Host-Key-Prüfung, Docker-/Compose-Unterstützung und lokale Offline-Hilfe.
Auch per SSH erreichbare VMs und Container können als Hosts verwaltet werden.

Stabile Veröffentlichung: **v1.2.4** · Aktueller Entwicklungsstand: **v1.2.5-beta6**
[English README](README_EN.md)

## Features

- Hostliste mit Status, Update-Zähler und Protokollausgabe
- Updates prüfen, simulieren und installieren; Systeme neu starten
- Nicht mehr benötigte Pakete auf Debian-basierten Systemen bereinigen
- Unterstützung für Debian/Ubuntu, Fedora/RHEL und Arch sowie ausgewählte Derivate
- Hostverwaltung mit Passwort- oder SSH-Key-Authentifizierung
- Master-Passwort zum Schutz gespeicherter SSH-Passwörter
- Themes: **Hell**, **Dunkel**, **Standard** und **Colour**; das gespeicherte Theme
  wird beim Start automatisch angewendet
- Lokale Anwendungsdaten unter `~/.sshupdater/`
- Docker Engine/Compose erkennen, eingerückte Projektzeilen mit unabhängiger
  Host-/Projektauswahl und Docker-Detailansicht
- Read-only-Imageprüfung desselben konfigurierten Registry-Tags, konservative
  Statusanzeige bei unklarem Vergleich und kontrollierter Updateablauf
- Registry-Metadaten ausschließlich im RAM zwischenspeichern; Rate-Limits beachten
- Offline-Hilfe zu Einrichtung, SSH, Systemaktionen, Docker, Sicherheit und Fehlerbehebung

## Oberfläche

Die Hosttabelle ordnet Compose-Projekte ihren Hosts unter. Die Toolbar besitzt
einen eigenen Container-Bereich und den Eintrag **Hilfe**.

![SSH Updater mit aufgeklappten Docker-/Compose-Projekten im Colour-Theme.](src/sshupdater/assets/ssh_updater.png)

*SSH Updater mit aufgeklappten Docker-/Compose-Projekten im Colour-Theme.*

## Installation / Quickstart

### Release-Binary

- **Linux x86_64:** `.tar.gz`-Releasearchiv entpacken und `./ssh-updater` starten.
- **Windows x86_64:** `.zip`-Releasearchiv entpacken und `ssh-updater.exe` starten.

Eine grafische Desktop-Umgebung ist erforderlich. Aus diesen Buildzielen folgt
keine Garantie für jede Linux-Distribution oder Windows-Version. Die obige
Entwicklungsversion bedeutet nicht, dass ein Beta-Releasearchiv veröffentlicht ist.

### Quellcode unter Linux

Die Python-Version steht in [release-python.txt](release-python.txt), derzeit
**3.11.x**. Im Projektverzeichnis:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
./run_dev.sh
```

Umgebung und Abhängigkeiten zuerst einrichten: `run_dev.sh` übernimmt beides nicht.

Auf den Zielsystemen müssen SSH und der passende Paketmanager verfügbar sein.
Administrative Befehle benötigen entsprechende Rechte; für Aufrufe über `sudo`
muss die Ausführung ohne Passwortabfrage möglich sein. Auf Arch wird für die
Update-Prüfung `checkupdates` aus `pacman-contrib` benötigt. APT muss die Option
`--error-on=any` unterstützen; Reboot benötigt `systemd-run`.

### Erster Start / Master-Passwort

Beim ersten Start ein Master-Passwort festlegen und bestätigen. Bei späteren
Starts den lokalen Vault damit entsperren. Dies ist auch bei ausschließlicher
SSH-Key-Nutzung erforderlich. Das Master-Passwort schützt lokal verschlüsselte
Zugangsdaten; Einzelheiten stehen in der In-App-Hilfe.

## Grundlegende Bedienung

1. **Konfiguration** öffnen und Hosts mit Adresse, Benutzer, Port sowie Passwort
   oder SSH-Key hinzufügen.
2. In **Konfiguration → Serveridentität prüfen** ausgewählte Hostzeilen prüfen;
   ohne Auswahl werden alle noch unbestätigten Endpunkte geprüft. Die Prüfung
   führt keinen Login aus und erzeugt kein Vertrauen. Fingerprints unabhängig
   vergleichen und nur die gewünschten Endpunkte ausdrücklich bestätigen.
   Geänderte Keys erfordern zusätzlich eine einzelne Warnbestätigung.
3. Im Hauptfenster die gewünschten Hosts auswählen und mit **Prüfen** nach Updates
   suchen. **Simulieren** zeigt eine Vorschau, ohne Paket-Upgrades auszuführen.
   Bei DNF und Arch ist dies eine Updateübersicht, kein vollständiger Transaktionsplan.
4. Mit **Upgrade** Updates installieren. **Bereinigen** simuliert auf Debian-basierten
   Systemen zunächst das Entfernen ungenutzter Pakete und verlangt eine Bestätigung.
   **Reboot** plant einen Neustart des Zielsystems.
5. Ergebnisse und Fehlermeldungen im Protokoll prüfen.

**Prüfen** und **Simulieren** können Paketlisten aktualisieren, installieren dabei
aber keine Pakete. System-Upgrades können Docker Engine/Compose als Hostpakete
aktualisieren; Compose-Images und Container verwenden den separaten Ablauf unten.

**Stopp** beendet das lokale Warten und überspringt weitere ausgewählte Hosts.
Ein bereits gestarteter Remote-Prozess kann weiterlaufen. Auch nach einem Timeout
oder Verbindungsabbruch kann der Zustand am Ziel unbekannt sein; vor einem erneuten
Start dort prüfen, ob die Aktion noch läuft oder bereits abgeschlossen ist.

## Docker / Compose

Nach einer Hostprüfung erscheinen erkannte Compose-Projekte eingerückt unter
ihrem Host. Hostcheckboxen wählen Systemaktionen, Projektcheckboxen Docker-/
Compose-Aktionen. Keine Auswahl setzt die andere automatisch. Ein Klick auf die
Docker-Anzeige öffnet Details der letzten Prüfung ohne erneute Remote-Abfrage.

Die Imageprüfung vergleicht das laufende Image mit **demselben konfigurierten
Tag**: `nginx:1.28-alpine` gegen `nginx:1.28-alpine`, ohne automatisch
`nginx:1.29-alpine` auszuwählen. Ein unklarer Vergleich ergibt eine unvollständige
Prüfung statt einer geratenen Aktualitäts- oder Updateaussage.

1. Compose-Projekt auswählen und **Update-Vorschau** öffnen: ein read-only Plan
   aus der letzten Prüfung, keine Docker-Simulation.
2. **Docker-Update** führt einen frischen Live-Preflight durch und pullt danach
   nur freigegebene Update-Services. Der Pull ersetzt noch keine Container.
3. **Docker anwenden** prüft den vorbereiteten Zustand erneut und erstellt diese
   Services mit dem geladenen Image neu, ohne erneuten Pull oder Build.
4. **Docker prüfen** verifiziert lokale Daten und Registry-Stand abschließend
   read-only. Erst ein eindeutiges Ergebnis schließt die Verifikation ab.

Ein späterer Registryfehler macht einen erfolgreichen Apply nicht rückwirkend
ungültig. Die Verifikation bleibt ausstehend; ein erneut geänderter Registry-Tag
kann stattdessen einen neuen Updatekandidaten ergeben. Registry-Metadaten liegen
nur im Sitzungs-RAM (30 Minuten TTL), lokale Containerdaten werden frisch gelesen.
Rate-Limit-Backoff wird respektiert; ein Neustart verwirft Cache und vorbereitete
Updatezustände.

### Voraussetzungen und Grenzen

Docker Engine, Compose und Buildx für Registry-Metadatenprüfungen müssen für den
SSH-Benutzer nichtinteraktiv verfügbar sein. Compose wird bewusst konservativ
unterstützt: eine eindeutige lokale Compose-Datei und geeignete `image:`-
Referenzen, auch mit normaler lokaler Projekt-`.env` im selben Verzeichnis.
Externe/mehrere Env-Dateien, Service-`env_file`, Shell-Anwendungsvariablen sowie
Overrides, Includes, Extends, Profile und komplexe Interpolation bleiben
ausgeschlossen. Details stehen in der Hilfe und den
[technischen Docker-Dokumenten](docs/docker-image-updates.md).
Swarm und Kubernetes gehören nicht zum normalen unterstützten Compose-Updatepfad.

Keine automatische Auswahl höherer Tags, Compose-Dateiänderung, Änderung von
Digest-Pins, lokale Image-Builds, Registry-Logins, `docker compose down`, Image-/
System-Prune, Löschung alter Images, Rollbacks oder anwendungsspezifische Backups/
Migrationen. Vorhandene Registry-Zugänge auf dem Zielhost kann Docker selbst
verwenden; der Metadaten-Cache speichert keine Zugangsdaten.

**Anwendungsvorbereitung bleibt erforderlich:** Ein verfügbares Imageupdate ist
keine Zusicherung, dass die Anwendung sofort aktualisiert werden darf.
Zustandsbehaftete Dienste können Backups, Snapshots, Migrationen, die Prüfung der
Release Notes und eine vorgeschriebene Upgrade-Reihenfolge benötigen. SSH Updater
automatisiert den technischen Compose-Image-Ablauf; die Upgradehinweise der
Anwendung bleiben maßgeblich.

## Lokale In-App-Hilfe

Über **Hilfe** steht eine vollständig lokale Offline-Anleitung mit 20 Themen zu
Einrichtung, Hosts, SSH, Systemaktionen, Docker/Compose, Sicherheit und
Fehlerbehebung bereit. Dafür ist keine Internetverbindung erforderlich.
Die ausführliche Bedienung steht dort und wird hier nicht vollständig wiederholt.

## Einsatzbereich und Sicherheit

SSH Updater ist für eigene oder vertrauenswürdige Systeme innerhalb eines
**kontrollierten, sicheren LANs** vorgesehen. Es ist kein öffentliches
Internet-Admin-Portal, Multi-Tenant-System, Zero-Trust-Gateway oder
Bastion-/Jump-Host-Ersatz.

Ein vertrauenswürdiges LAN ersetzt weder SSH-Key-Schutz und Host-Key-Prüfung
noch angemessene Rechte oder Backups/Snapshots vor kritischen Änderungen.

Das Tool enthält angemessene Schutzmaßnahmen für diesen Einsatzzweck. Es ist jedoch
keine Hochsicherheitslösung für bereits kompromittierte Clients oder feindliche
lokale Umgebungen. Angreifer mit vollständigem Zugriff auf das Benutzerkonto, den
laufenden Prozess oder lokale Anwendungsdateien liegen außerhalb des vorgesehenen
Bedrohungsmodells. Konkrete, nachvollziehbare Sicherheitsprobleme werden weiterhin
ernst genommen und nach Möglichkeit behoben.

## Sicherheit in Kürze

- Serveridentitäten werden per Host-Key-Prüfung kontrolliert. Unbekannte oder
  geänderte Schlüssel müssen vor einer Anmeldung ausdrücklich geprüft und bestätigt
  werden, etwa nach einer Neuinstallation des Servers.
- Passwort- und SSH-Key-Authentifizierung werden unterstützt. Neu gespeicherte
  Passwörter werden verschlüsselt und an die zugehörige Hostkonfiguration mit Ziel,
  Benutzer und Port gebunden. Bei bestehenden älteren Passwörtern ist vor der
  nächsten Passwortanmeldung einmalig eine erneute Eingabe erforderlich.
- Agent- und X11-Forwarding werden für die von der Anwendung selbst aufgebauten
  SSH-Verbindungen deaktiviert.
- Remote-Ausgaben werden als Klartext behandelt. Aktionen haben Zeit- und
  Ausgabelimits und können lokal mit **Stopp** beendet werden.

Root ist nicht allgemein erforderlich. Für bewusst administrierte Docker-Hosts
in dieser kontrollierten Umgebung ist **root + SSH-Key** eine robuste unterstützte
Konfiguration, keine Empfehlung für Root-Passwort-Login oder pauschale sudo-Rechte.
Docker-Daemon-Zugriff verleiht weitreichende Hostrechte: Socket/API nicht unnötig
im Netzwerk exponieren. SSH Updater nutzt den konfigurierten SSH-Zugang.
Private Keys schützen und unbekannte/geänderte Host-Keys niemals blind bestätigen.

## Entwicklung / Build

Ein lokales Standalone-Binary unter Linux erstellen:

```bash
./run_erstelle.sh
```

Das Skript verwendet eine separate Python-3.11-Umgebung unter `.venv-release`,
installiert die festgelegten Abhängigkeiten, führt die Tests aus und erstellt mit
PyInstaller `dist/ssh-updater`. Anschließend prüft es das Binary mit einem Smoke-Test.
Nur das Ergebnis eines erfolgreich abgeschlossenen Builds verwenden.

Die Tests lassen sich in der eingerichteten Build-Umgebung separat starten:

```bash
.venv-release/bin/python -B scripts/test_release.py
```

Abhängigkeiten stehen in [requirements.txt](requirements.txt) und
[requirements-build.txt](requirements-build.txt).

## Technische Dokumentation

Architektur- und Sicherheitsdetails für Entwickler; die primäre
Benutzeranleitung befindet sich in der Anwendung.

- [Compose-Erkennung](docs/docker-compose-discovery.md)
- [Imageprüfung und Registry-Sitzungscache](docs/docker-image-updates.md)
- [Live-Preflight](docs/docker-preflight.md)
- [Gezielter Image-Pull](docs/docker-image-pull.md)
- [Apply und Containerkontrolle](docs/docker-apply.md)
- [Abschlussverifikation](docs/docker-verification.md)
- [SSH-Transportlimits](docs/transport-limit.md)

## Projekt und Mitwirkende

Projektverantwortung: **Holger Mangold**. Mitwirkung / Unterstützung: **Calimero**.
Der Autorenhinweis der GUI lautet `© @Faber38 / © @CalimerO`.

Bei Entwicklung, Codeanalyse, Tests und Dokumentation wurden KI-gestützte
Werkzeuge unterstützend eingesetzt. Entscheidungen, Prüfung und Freigabe der
Änderungen verbleiben beim Projektverantwortlichen.

## Mögliche zukünftige Erweiterungen

Unverbindliche Ideen ohne Releasezusage:

- Headless-Betrieb auf einem Proxmox-Host
- Log-Archivierung und Export
- Optionale Statusmeldungen via Telegram

## Danksagung

Besonderer Dank geht an **Calimero078** für die intensive Unterstützung bei
der Analyse und Härtung der SSH- und Credential-Sicherheit. Seine Tests,
Reviews und sein eigener Security-Hardening-Entwurf haben wesentlich zur
Weiterentwicklung der Host-Key-Prüfung, des authentifizierten Trust-Stores
und der kontrollierten Credential-Freigabe beigetragen.

Ebenfalls vielen Dank an alle, die den SSH Updater testen und mit konkreten
Fehlerberichten und ungewöhnlichen Konfigurationen dabei helfen, ihn robuster
zu machen.

## Lizenz

MIT License – siehe [LICENSE](LICENSE).

Copyright (c) 2025 Holger Mangold


### Serveridentität und Verbindungsdaten

Host/IP, Port, Benutzer und expliziter Key-Pfad aus SSH Updater sind maßgeblich.
Die Datei `~/.ssh/config` wird nicht automatisch geladen. SSH-Konfigurationsaliase,
HostKeyAlias, ProxyJump, ProxyCommand sowie dortige IdentityFile-/CertificateFile-
und Komfortoptionen entfallen bewusst. Verwenden Sie direkt erreichbare IPs oder
DNS-Namen. Ohne expliziten Key-Pfad bleiben Standardschlüssel und SSH-Agent für
normale Key-Anmeldungen verfügbar; die Identitätsprüfung nutzt keinerlei Credentials.

Hosteinträge mit gleicher Adresse und gleichem Port teilen einen bestätigten Pin.
Der app-eigene Trust-Store ist per HMAC an den bestehenden Vault gebunden. Ein alter
Store ohne MAC verlangt eine ausdrückliche Neubestätigung: alte Dateien werden
inaktiv und ohne Überschreiben früherer Sicherungen quarantänisiert, danach wird
mit einem leeren authentifizierten Store begonnen. Recovery ist auch beim Start
möglich. Ungültige Store-/MAC-Paare blockieren SSH; vorhandene Pins werden niemals
automatisch neu signiert. Das Vault- und Credential-v2-Format bleibt unverändert.

Dieser Schutz ist für das kontrollierte private LAN vorgesehen. Er verhindert
keinen Replay eines früher gültigen Store-/MAC-Paars. Backups müssen das
zusammengehörige Dateipaar und den zugehörigen Vault enthalten.
