<p align="center">
  <img src="src/sshupdater/assets/icon.png" alt="SSH Updater Icon" width="120"/>
</p>

# SSH Updater

SSH Updater ist eine grafische Anwendung zum zentralen Verwalten und Aktualisieren
mehrerer Linux-Systeme über SSH. Dazu gehören auch VMs und Container, etwa auf
Proxmox, sofern sie per SSH erreichbar sind. Die Qt-Oberfläche zeigt Status,
verfügbare Updates und die Ausgabe laufender Aktionen an.

Aktuelle Version: **1.2.4** · [English README](README_EN.md)

## Features

- Hostliste mit Status, Update-Zähler und Protokollausgabe
- Updates prüfen, simulieren und installieren; Systeme neu starten
- Nicht mehr benötigte Pakete auf Debian-basierten Systemen bereinigen
- Unterstützung für Debian/Ubuntu, Fedora/RHEL und Arch sowie ausgewählte Derivate
- Hostverwaltung mit Passwort- oder SSH-Key-Authentifizierung
- Master-Passwort zum Schutz gespeicherter SSH-Passwörter
- Themes: Hell, Dunkel und Colour
- Lokale Anwendungsdaten unter `~/.sshupdater/`

## Screenshots

### Hauptfenster

<p align="center">
  <img src="src/sshupdater/assets/ssh_updater.png" alt="Hostliste mit Status, Update-Zähler und Protokollausgabe" width="800"/>
</p>

### Konfiguration

<p align="center">
  <img src="src/sshupdater/assets/Konfig.png" alt="Dialog zum Hinzufügen und Bearbeiten von Hosts" width="600"/>
</p>

## Installation / Quickstart

Für den Start aus dem Quellcode werden Python **3.11.x** und eine grafische
Desktop-Umgebung benötigt. Im Projektverzeichnis unter Linux:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
./run_dev.sh
```

Auf den Zielsystemen müssen SSH und der passende Paketmanager verfügbar sein.
Administrative Befehle benötigen entsprechende Rechte; für Aufrufe über `sudo`
muss die Ausführung ohne Passwortabfrage möglich sein. Auf Arch wird für die
Update-Prüfung `checkupdates` aus `pacman-contrib` benötigt. APT muss die Option
`--error-on=any` unterstützen; die Reboot-Funktion benötigt `systemd-run`.

## Grundlegende Bedienung

1. **Konfiguration** öffnen und Hosts mit Adresse, Benutzer, Port sowie Passwort
   oder SSH-Key hinzufügen.
2. Für jeden neuen Host **Serveridentität prüfen** öffnen. Den angezeigten
   Fingerprint mit der Serverkonsole oder einer unabhängig geprüften Quelle
   vergleichen und erst bei Übereinstimmung bestätigen.
3. Im Hauptfenster die gewünschten Hosts auswählen und mit **Prüfen** nach Updates
   suchen. **Simulieren** zeigt eine Vorschau, ohne Paket-Upgrades auszuführen.
   Bei DNF ist dies eine Liste verfügbarer Updates, kein vollständiger Transaktionsplan.
4. Mit **Upgrade** Updates installieren. **Bereinigen** simuliert auf Debian-basierten
   Systemen zunächst das Entfernen ungenutzter Pakete und verlangt eine Bestätigung.
   **Reboot** plant einen Neustart des Zielsystems.
5. Ergebnisse und Fehlermeldungen im Protokoll prüfen.

**Stopp** beendet das lokale Warten und überspringt weitere ausgewählte Hosts.
Ein bereits gestarteter Remote-Prozess kann weiterlaufen. Auch nach einem Timeout
oder Verbindungsabbruch kann der Zustand am Ziel unbekannt sein; vor einem erneuten
Start dort prüfen, ob die Aktion noch läuft oder bereits abgeschlossen ist.

## Einsatzbereich und Sicherheit

SSH Updater wurde entwickelt, um eigene und vertrauenswürdige Linux-Systeme, VMs
und Container im LAN beziehungsweise in administrierten Netzwerken bequem zentral
zu aktualisieren. Viele dieser Aufgaben könnten auch einzelne Shell-Skripte
erledigen; die Anwendung fasst sie in einer grafischen Oberfläche zusammen.

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
[requirements-build.txt](requirements-build.txt). Ergänzende technische Hinweise
zum SSH-Transport stehen in [docs/transport-limit.md](docs/transport-limit.md).

## Roadmap

- Headless-Betrieb auf dem Proxmox-Host
- Log-Archivierung und Export
- Optionale Statusmeldungen via Telegram

## Lizenz

MIT License – siehe [LICENSE](LICENSE).
