# v1.2.5-beta6: Freigabevorbereitung

Stand: 2026-10-02. Anwendung: `1.2.5-beta6`; Stable bleibt `v1.2.4`.
Dies ist eine gezielte Testversion für Host-Key-Prüfung, Trust-Store und
Mehrhost-Bedienung im kontrollierten Trusted-LAN-Modell.

## Eingefrorener Funktionsumfang

- Ohne Auswahl: alle noch unbestätigten Endpunkte; mit expliziter Auswahl:
  ausschließlich diese Endpunkte. Gleiche Adresse und gleicher Port teilen einen Pin.
- Credential-freie Inspektion, Fehlerisolation je Host und gemeinsame Ergebnisanzeige.
  Die Prüfung allein speichert kein Vertrauen; keine Vertrauensentscheidung ist
  vorausgewählt. Geänderte Keys benötigen eine zusätzliche Einzelbestätigung.
  Veraltete Ergebnisse dürfen bestehende Pins nicht überschreiben.
- App-eigener known_hosts-Store mit HMAC-SHA256; zweckgetrennte HKDF-Ableitung
  aus dem bestehenden Vault-Schlüssel. Legacy-/Integritäts-Recovery erfordert
  ausdrückliche Zustimmung, sichert alte Dateien inaktiv und beginnt ohne Pins.
- Passwortbereitstellung erst nach erfolgreicher Host-Key-Prüfung;
  begrenzte Password-/Keyboard-Interactive-Versuche.
- Produktiv `config=[]`: gespeicherte Host/IP-, Port-, User- und Key-Werte sind
  autoritativ. `~/.ssh/config` wird nicht automatisch als Routingquelle verwendet.

Keine Zusage von Zero Trust oder Schutz gegen Replay eines früher gültigen
Store-/MAC-Paars. Der bestehende begrenzte LAN-Vertrauensrahmen bleibt maßgeblich.

## Bestätigter LAN-Realtest

Der Projektverantwortliche hat den heutigen Realtest im kontrollierten LAN als
erfolgreich bestätigt. Dieser Nachweis dokumentiert seinen Bericht; während
dieser Vorbereitung wurden keine echten Hosts kontaktiert.

- Alter Trust-Store ohne MAC erkannt, Pins nicht automatisch übernommen,
  Recovery ausdrücklich bestätigt; anschließend normaler Hauptfensterstart
  mit funktionierendem leerem authentifiziertem Store.
- Ohne Auswahl 13/13 unbestätigte Endpunkte geprüft. Ein unerreichbarer Host
  erschien als Einzelfehler; die übrigen Hosts wurden weiter geprüft.
- Kein Fingerprint vorausgewählt; Prüfung allein speicherte kein Vertrauen.
  `dockertest` wurde einzeln ausdrücklich bestätigt. Der nächste Lauf ohne
  Auswahl prüfte nur noch 12 Endpunkte und ließ diesen Host korrekt aus.
- Explizite Auswahl zweier Hosts führte exakt zu 2/2 Prüfungen.
  Anschließende normale Key-SSH-Prüfung von `dockertest`, Systemupdate-Erkennung
  und Docker-Discovery funktionierten weiterhin.
- Weitere Auffälligkeiten waren Host-/Umgebungsprobleme: NextcloudPi hatte einen
  defekten/abgelaufenen externen APT-Repository-Key; sFTPgo eine veraltete
  Repository-Adresse beziehungsweise einen Repository-Key; watchYourLan eine
  absichtliche Internet-Sperre durch eBlocker. Dabei wurde kein echter
  Trust-/SSH-Codefehler gefunden. Daraus entstanden keine Repositoryänderungen.

## Unveränderte Grenzen

PBKDF2, `vault.salt`, `vault.verify`, bestehendes Fernet-Format, Credential-v2
und DB-Schema bleiben unverändert. Es gibt keinen neuen Vault-Master-Key,
keine Vault-Migration, keine Credential-Neuverschlüsselung, kein persistentes
Throttling und keine neue Master-Passwort-Policy. Keine Umstellung auf scrypt
und keine neue KDF-Metadatendatei.

Docker bleibt seit beta5 funktional unverändert: Discovery, Image-Updates,
.env-Unterstützung, Pull, Apply, Verification und Compose-Policy. Die vier
zusätzlich gelieferten Compose-Fälle bleiben einem späteren Arbeitsschritt
vorbehalten. Die Änderung in der Discovery-Dokumentation erläutert lediglich
die nun autoritativen direkten SSH-Ziele.

## Lokale Verifikation

Release-Umgebung mit `scripts/release.py environment` verifiziert:
Python **3.11.16**, einschließlich der gepinnten Abhängigkeiten.
Vollständiger Lauf: `.venv-release/bin/python -B scripts/test_release.py`.
**478 Tests erfolgreich, keine Fehler und keine übersprungenen Tests.**
Der erste Sandbox-Lauf war wegen gesperrter lokaler Sockets unbrauchbar;
der vollständige erfolgreiche Lauf erfolgte mit erlaubten Loopback-Sockets.

| Bereich (Teilmenge der Gesamtsuite) | Tests |
| --- | ---: |
| Trust, Mehrhost und Recovery | 33 |
| Trust-Store-/SSH-Redirect | 8 |
| Credential-v2 | 49 |
| SSH-/Storage-Security | 33 |
| Security-GUI | 3 |
| Vault-Initialisierung | 12 |
| Summe dieser Trust-/Security-Bereiche | 138 |
| Docker-Regressionen (`test_docker_*`) | 222 |
| Registry-Sitzung (zusätzlich) | 12 |
| Hilfe und Themes | 18 |
| Release-/Tagtests | 16 |

Offline-GUI-Smoke erfolgreich (`python -m sshupdater.app --smoke-test`).
Separater Mehrhost-Dialog-Smoke mit Qt-Worker und Ereignisschleife erfolgreich:
2/2 synthetische Ergebnisse einschließlich Einzelfehler, keine Vorauswahl und
kein automatisches Vertrauen, ausdrückliche Pin-Bestätigung, anschließende
Filterung sowie explizite Auswahl bestätigt. Beide Smokes liefen offscreen.

Exakte Zuordnung `1.2.5-beta6` ↔ `v1.2.5-beta6` gültig; `prerelease=true`.
Stable-Verhalten unverändert. Release-/Taglogik und Workflow unverändert;
Testmatrix um beta5/beta6 ergänzt. Workflow-YAML erfolgreich geparst.

## Diff- und Git-Audit

Gesamter offener Diff gegen HEAD einschließlich neuer Tests geprüft.
Keine Docker-Nacharbeiten, echten LAN-Adressen aus dem Realtest, echten
Fingerprints oder Credentials, Quarantäne-/Backup-Dateien, Debugausgaben,
temporären Dateien, Merge-Marker oder Review-Branch-Artefakte enthalten.
Die vorhandene private Beispieladresse in der Hilfe ist unverändert;
Test-Credentials sind synthetisch und Testschlüssel werden zur Laufzeit erzeugt.
Die negativen Tests gegen eine neue KDF-Datei und der Herkunftskommentar zur
Schlüsseltrennung sind beabsichtigt. Bestehende Autorenangaben bleiben erhalten.
Die neue DE/EN-Danksagung an **Calimero078** übernimmt den freigegebenen Wortlaut
und behauptet keine vollständige PR-Übernahme.

`git diff --check` sauber. 20 geänderte und 3 neue Dateien, nichts gestagt,
keine unerwünschten unversionierten Dateien. Bestehende Tags einschließlich
`v1.2.5-beta5` unverändert. Kein Commit, Push, Tag oder Release durchgeführt.
