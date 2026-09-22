<p align="center">
  <img src="src/sshupdater/assets/icon.png" alt="SSH Updater Icon" width="120"/>
</p>

# SSH Updater

Ein grafisches Tool zum **Verwalten und Aktualisieren mehrerer SSH-Server oder Proxmox-Container** über eine zentrale Qt-Oberfläche.  
Ideal für Administratoren, die mehrere Systeme regelmäßig prüfen, simulieren und updaten möchten.

---

## ✨ Features
- Übersichtliche Hostliste mit Online-/Offline-Status  
- Aktionen: **Prüfen**, **Simulieren**, **Upgrade**, **Bereinigen**, **Reboot**  
- Konfigurationsdialog mit Hostverwaltung und Passwortschutz  
- Mehrere Themes: Hell, Dunkel, Colour  
- Lokale Datenbank im Benutzerverzeichnis (`~/.sshupdater/`)  
- Unterstützt Passwort- und SSH-Key-Authentifizierung  

---

## 🖥️ SSH Updater – Hauptfenster

<p align="center">
  <img src="src/sshupdater/assets/ssh_updater.png" alt="SSH Updater Hauptfenster" width="800">
  <br>
  <em>Übersicht aller Hosts mit Status, Update-Zähler und Log-Ausgabe</em>
</p>

---

## ⚙️ Konfigurationsansicht

<p align="center">
  <img src="src/sshupdater/assets/Konfig.png" alt="SSH Updater Konfiguration" width="600">
  <br>
  <em>Dialog zum Bearbeiten, Hinzufügen und Löschen von Hosts</em>
</p>

---

## 🚀 Quickstart (Entwicklung)

```bash
# Virtuelle Umgebung anlegen und aktivieren
python3.11 -m venv .venv
source .venv/bin/activate

# Abhängigkeiten installieren
pip install -r requirements.txt

# Start (Entwicklermodus)
./run_dev.sh
```

Oder als **Standalone-Build**:

```bash
./run_erstelle.sh
# Ausführbare Datei unter dist/ssh-updater
```

---

## 📌 Roadmap
- Der SSH-Updater soll auch headless auf dem Proxmox-Host laufen.  
- Log-Archivierung und Export  
- Optionale Statusmeldungen via Telegram  

---

## 📄 Lizenz
MIT License – siehe [LICENSE](LICENSE)

## Sicherheit und Update auf 1.2.0

SSH Updater ist für einen persönlich genutzten Rechner und verwaltete Systeme
im privaten LAN vorgesehen. Auch dort wird die Identität jedes SSH-Servers geprüft.

### Einmalige Serverbestätigung

1. In **Konfiguration** den Host auswählen und **Serveridentität prüfen** öffnen.
2. Adresse, Port und SHA256-Fingerprint mit der lokalen Konsole des Servers oder
   einer unabhängig geprüften Quelle vergleichen. Auf der Serverkonsole kann
   `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256` den Fingerprint
   anzeigen; für einen anderen angezeigten Schlüsseltyp dessen öffentliche
   Host-Key-Datei verwenden. Niemals den privaten Host-Key kopieren.
3. Nur bei Übereinstimmung das Kontrollkästchen aktivieren und bestätigen.
4. Die gewünschte SSH-Aktion anschließend selbst starten.

Die Prüfung meldet sich nicht am Ziel an und führt dort keine Befehle aus.
Bei ProxyJump kann sie sich an bereits bestätigten Sprungservern mit lokalen
SSH-Keys anmelden, um das Ziel zu erreichen. Zuerst gegebenenfalls den
Sprungserver bestätigen, dann erneut prüfen, bis auch das Ziel bestätigt ist.

Unbekannte und geänderte Keys blockieren sämtliche SSH-Aktionen **vor der
Anmeldung am betreffenden Server**. Nach einer Server-Neuinstallation muss ein
geänderter Fingerprint erneut unabhängig geprüft und ausdrücklich ersetzt werden.
Es gibt keine automatische Übernahme und keinen automatischen Neustart einer
abgebrochenen Aktion.

Vertrauen wird pro Host/Port in `~/.sshupdater/known_hosts` gespeichert.
`~/.ssh/known_hosts` wird weder verändert noch automatisch importiert. Die neue
Datei enthält konkrete öffentliche Host-Key-Pins im OpenSSH-Format; allgemeine
Wildcard-/CA-Einträge sind dort nicht vorgesehen. Server müssen einen gewöhnlichen
öffentlichen SSH-Host-Key anbieten können.

### Bestehende SSH-Konfiguration

`~/.ssh/config` bleibt aktiv, einschließlich der von AsyncSSH unterstützten
`HostName`-Aliase, `IdentityFile`, `CertificateFile`, `IdentityAgent`,
`IdentitiesOnly`, `HostKeyAlias`, ProxyJump/ProxyCommand und Verbindungsoptionen.
Benutzer und Port aus der Host-Konfiguration des Programms haben wie bisher Vorrang.
Ein `HostKeyAlias` wird als Vertrauensname angezeigt, zusätzlich zur tatsächlichen
Verbindungsadresse. Die SSH-Konfiguration und darin genannte Proxy-Programme sind
vertrauenswürdige lokale Konfiguration; Änderungen daran können das Ziel ändern.

SSH Updater erzwingt seine eigene Host-Key-Prüfung und deaktiviert Agent- und X11-Forwarding (auch bei ProxyJump),
Hostbased- und GSS-Authentifizierung. Passwortanmeldung verwendet ausschließlich
das gespeicherte Passwort (auch für einfache Keyboard-Interactive-Passwortabfragen).
Ein ausdrücklich gewählter Key-Pfad verwendet nur diese Identität einschließlich
eines zugehörigen Zertifikats aus `CertificateFile` oder der automatischen Suche; andere
konfigurierte Identitäten und der Agent werden in diesem Modus nicht genutzt.
Ohne Key-Pfad bleiben konfigurierte/Standard-Keys und der lokale Agent nutzbar.
Passwortfallback findet im Key-Modus nicht statt. Verschlüsselte Schlüssel können
über den lokalen Agenten bereitgestellt werden; eine neue Passphrase-Verwaltung
ist nicht Bestandteil dieses Releases.

ProxyJump-Verbindungen werden ebenfalls geprüft und verwenden Key-/Agent-Anmeldung
mit der SSH-Konfiguration des Sprungservers, niemals das Zielpasswort. Die
Anwendung kontrolliert die SSH-Sitzungen, die sie selbst aufbaut; separat gestartete
Programme in `ProxyCommand` behalten ihre eigenen Einstellungen.

### Passwörter, Dateien und Kompatibilität

Bei Änderung von Host/IP, Benutzer oder Port muss ein gespeichertes Passwort erneut
eingegeben oder ausdrücklich gelöscht werden. Beim Wechsel von Passwort auf Key
kann es bei unverändertem Ziel bewusst verschlüsselt behalten werden.

Vault-Format, PBKDF2-Parameter, Master-Passwort und Datenbankschema bleiben unverändert.
Es gibt keine automatische Credential-Löschung oder Neuverschlüsselung beim Update.
Unvollständige Vaults werden nicht stillschweigend neu angelegt.
Unter POSIX werden `~/.sshupdater` auf `0700` und bekannte Anwendungsdateien auf `0600`
begrenzt, auch bei bestehenden Installationen. Fremde Eigentümer, Symlinks und
mehrfach verknüpfte Dateien führen zum Abbruch statt zu automatischer Besitzübernahme.
Verwendete Symlinks müssen vor dem Update bewusst durch ein reguläres Datenverzeichnis
ersetzt werden; die Anwendung verschiebt keine Daten. Die POSIX-Prüfung ist keine
Windows-ACL-Härtung.

Die kryptografische Bindung von Credentials an Verbindungsdaten, eine speicherharte
KDF samt versionierter Migration, allgemeine Credential-Löschung, weitergehende Log-Härtung
und automatische Vault-Sperre bleiben einem späteren Release vorbehalten.

### Tests

Die Release-Konfiguration ist Python **3.11.x** (`release-python.txt`; getestet mit 3.11.16),
PyQt6 **6.11.0** mit Qt **6.11.2**, AsyncSSH **2.24.0 mit gepinntem Upstream-Transport-Fix**, Cryptography **50.0.1** und PyInstaller **6.22.3**.
Runtime-Abhängigkeiten einschließlich transitiver Pakete sind in `requirements.txt`,
Build-Pakete in `requirements-build.txt` festgelegt. Eine saubere Umgebung mit dieser
Python-Version erstellen und `python -m pip install -r requirements-build.txt` ausführen.
`run_erstelle.sh` prüft die Python-Version und führt vor dem Build die Tests aus;
der Linux-/Windows-Release-Workflow verwendet dieselben Versionen und Tests.
Die Pins definieren die Paketkonfiguration, keine bitidentischen Betriebssystem-Images.

AsyncSSH wurde wegen zusätzlicher Absicherung des Schlüsselaustauschs und
Proxy-/Konfigurationskorrekturen aktualisiert, nicht allein wegen Warnungen
([Änderungsprotokoll](https://asyncssh.readthedocs.io/en/latest/changes.html)).
Cryptography 50.0.1 vereinheitlicht die zuvor abweichenden lokalen und Release-Versionen;
seine Wheels enthalten OpenSSL 4.0.2
([Änderungsprotokoll](https://cryptography.io/en/latest/changelog/)).
Fernet-Format und PBKDF2 bleiben unverändert; der Legacy-Vault-Test prüft dies.

Externe `known_hosts`-Dateien vergeben kein Vertrauen innerhalb der Anwendung,
auch nicht für ProxyJump. Host-Zertifikate/CA-Vertrauen werden nicht unterstützt;
angeboten werden nur normale Public-Host-Key-Algorithmen, unter Berücksichtigung
der Einschränkungen aus `HostKeyAlgorithms`. Client-Zertifikate bleiben nutzbar.
Fehlende Vault-Dateien bei vorhandenen verschlüsselten Daten, beschädigte Salts
und nicht zum entsperrten Schlüssel passende Credentials führen zum Abbruch.
Die Anwendung löscht oder repariert diese Daten nicht automatisch. Eine abgebrochene
Erstinitialisierung mit nur einer Vault-Datei wird ebenfalls ausdrücklich abgewiesen.

`.venv-release/bin/python -B scripts/test_release.py`

Die Sicherheitstests verwenden temporäre Daten und lokale SSH-Testserver auf
`127.0.0.1`; sie benötigen die Erlaubnis, lokale Sockets zu öffnen. Sie führen keine
Administrationsbefehle auf verwalteten Systemen aus.

### Begrenzte Ausgaben und lokales Stoppen

Remote-Texte werden ausschließlich als Klartext angezeigt; HTML, Entities und
SVG-Daten-URLs werden nicht ausgewertet. Das Protokoll hält maximal 2.000 Absätze
und 16.384 Zeichen pro Eintrag. Ältere Einträge werden verworfen, überlange gekürzt.
SSH-Befehle haben ein gemeinsames stdout/stderr-Limit von 4 MiB bei Abfragen bzw.
16 MiB beim Streaming. Der Verbindungsaufbau einschließlich ProxyJump ist auf
20 Sekunden begrenzt. Abfragen warten höchstens 90 Sekunden (Reboot: 10 Sekunden),
Streaming-Befehle höchstens eine Stunde und höchstens fünf Minuten ohne Ausgabe.
Streaming-Zeilen werden vor der UI-Anzeige gebündelt.

**Stoppen beendet das lokale Warten und überspringt weitere ausgewählte Hosts.**
Timeout, Ausgabelimit oder Verbindungsabbruch nach Start einer administrativen
Aktion bedeuten „Remote-Zustand unbekannt“. Es wird kein Kill-/Terminate-Signal
gesendet; die SSH-Verbindung wird geschlossen. Der Remote-Prozess kann weiterlaufen
oder auf den Verbindungsabbruch reagieren. Vor erneutem Start am Ziel prüfen.

Fehlgeschlagene Simulationen werden als Fehler angezeigt. Autoremove startet erst
nach Bestätigung und nur auf erfolgreich simulierten Hosts. DNF zeigt verfügbare
Updates über `check-update` (Exitcodes 0/100), keinen vollständigen Transaktionsplan.
Arch `checkupdates` mit Exitcode 2 bedeutet „keine Updates“.

### Lokale Release-Builds

`run_erstelle.sh` erstellt/verwendet eine separate `.venv-release` mit Python 3.11,
installiert die Runtime-/Build-Pins und prüft Umgebung, Abhängigkeiten, Tests und
anschließend das erzeugte Binary mit `--smoke-test`. Über `SSH_UPDATER_PYTHON` und
`SSH_UPDATER_RELEASE_VENV` lassen sich Interpreter und isolierte Umgebung wählen.
Der Test-Runner isoliert Home-Zugriffe und Qt-Einstellungen. Der Offline-Smoke-Test
verwendet keine Vault-Dateien, SSH-Agenten oder verwalteten Hosts.

`dist/` ist ignorierter lokaler Buildbestand, kein versionierter Release-Nachweis.
Nur das Ergebnis eines vollständig erfolgreichen aktuellen Builds verwenden; ein
vorhandenes Binary kann aus einem älteren Build stammen. Die Release-Workflows
akzeptieren nur `vMAJOR.MINOR.PATCH` passend zur internen `__version__` und verwenden
diesen Tag für die Archivnamen. Nur der abschließende Release-Job hat Schreibrechte.

### Ergänzungen aus dem abschließenden Security-Review

AsyncSSH wird unverändert aus der freigegebenen Upstream-Revision
`459f44515238880b7be68299bb7d1fc0e704121d` gebaut, mit festem Archiv-SHA256.
Diese noch unveröffentlichte Revision meldet intern weiterhin 2.24.0; sie ist
nicht mit dem gleichnamigen PyPI-Paket austauschbar. Die Release-Prüfung
kontrolliert deshalb auch die installierte Archiv-Herkunft. Details und Grenzen:
[Transportlimit](docs/transport-limit.md).

Eigene Remote-Prozesse erzwingen `request_pty=False`, auch bei `RequestTTY force`.
Das Inaktivitätslimit umfasst Prozessanforderung, Ausgabe und Kanalschluss.
Alle sechs Aktionsworker lassen sich lokal stoppen; nach Abbruch beginnen keine
weiteren Hosts, und eine abgebrochene Autoremove-Simulation startet keine Bereinigung.
Fensterschließen fordert diesen lokalen Stopp an und wartet nur auf das Ende der
lokalen Worker. Es werden keine Kill-Signale an Remote-Paketmanager gesendet.
Ein unbekannter Remote-Ausgang bleibt als solcher gekennzeichnet.

APT-Indexupdates verwenden `--error-on=any`, damit auch vorübergehende Fehler
den nachfolgenden Schritt sperren. Alte APT-Versionen ohne diese Option scheitern
ausdrücklich; es gibt keinen unsicheren Fallback. Sonstige APT-Warnungen bleiben
in Prüfung und Simulation sichtbar.
