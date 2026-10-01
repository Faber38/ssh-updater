# Sicherheits-Härtung für v1.2.5

Diese Änderungen schließen die im Audit von v1.2.5-beta5 nachgewiesenen
Credential-Umleitungs- und Vault-Schwächen. Sie ändern bewusst das Verhalten bei
lokalen SSH-Konfigurationsdateien und beim ersten Start nach dem Upgrade.

## Serveridentität und Zugangsdaten

- `known_hosts` wird mit HMAC-SHA256 authentisiert. Der MAC-Schlüssel wird per
  HKDF aus dem entsperrten Vault-Schlüssel abgeleitet und getrennt verwendet.
- Fehlt das Siegel bei einem vorhandenen Truststore, übernimmt die Anwendung die
  bisherigen Einträge nicht automatisch. Nach ausdrücklicher Bestätigung wird
  die alte Datei als `known_hosts.untrusted` inaktiv gesichert; alle Server müssen
  über einen unabhängig verglichenen Fingerprint erneut bestätigt werden.
- Änderungen, Löschungen oder unvollständige Truststore-Zustände blockieren SSH.
  Ein fehlendes Siegel wird nie anhand des gerade gelesenen Inhalts neu erzeugt.
- Das SSH-Passwort wird erst entschlüsselt, nachdem der angebotene Host-Key mit
  dem authentisierten, exakten Host-/Port-Pin übereinstimmt. Vorherige Prüf- und
  Fehlerpfade erhalten keinen Klartext.
- Produktive Verbindungen verwenden Adresse, Port und Benutzer aus der
  Anwendungsdatenbank und laden keine benutzerspezifische OpenSSH-Konfiguration.
  Dadurch können `HostName`, `HostKeyAlias`, `ProxyJump` oder `ProxyCommand` ein
  gespeichertes Credential nicht auf ein anderes Ziel umlenken. Ein explizit in
  der Anwendung gewählter Key-Pfad bleibt unterstützt; ohne Key-Pfad können
  AsyncSSH-Standardschlüssel und der Agent verwendet werden.

## Vault

- Neue Vaults verwenden scrypt (`N=65536`, `r=8`, `p=1`) zum Umschließen eines
  zufälligen 256-Bit-Vault-Schlüssels. Neue Master-Passwörter benötigen mindestens
  zwölf Zeichen und vier verschiedene Zeichen.
- Bestehende PBKDF2-Vaults werden erst nach erfolgreichem Entsperren und Prüfung
  aller gespeicherten Credentials auf `vault.kdf` migriert. Die Credential-Tokens
  werden nicht neu geschrieben.
- Unbekannte KDF-Versionen, abweichende Parameter, beschädigte Metadaten und
  unvollständige Vault-Zustände werden abgelehnt.
- Fehlversuche werden lokal mit wachsender Wartezeit bis 30 Sekunden gedrosselt.
  Die Drosselung ist eine zusätzliche Hürde; ein Angreifer mit vollständiger
  Kontrolle über das Benutzerkonto oder den laufenden Prozess liegt weiterhin
  außerhalb des Bedrohungsmodells.

## Grenzen und Wiederherstellung

Das Integritätssiegel erkennt Änderungen an nur einer Datei sowie beliebige neue
Inhalte ohne den Vault-Schlüssel. Die gemeinsame Rücksetzung auf ein früheres,
damals gültiges Paar aus `known_hosts` und `known_hosts.mac` ist ohne externen
monotonen Zustand nicht erkennbar. Deshalb müssen lokale Backups und das
Benutzerkonto geschützt bleiben.

Bei einer Integritätsverletzung startet die Anwendung nicht in einen unsicheren
Zustand. Zuerst Ursache und Sicherungen prüfen. Eine absichtliche Neuinitialisierung
bedeutet immer, alle Server-Fingerprints erneut über eine unabhängige Quelle zu
verifizieren.

## Prüfungen

Die Regressionstests decken unter anderem Truststore-Manipulation und -Löschung,
fehlende beziehungsweise beschädigte MAC-Dateien, erneute Prüfung unmittelbar vor
der Credential-Freigabe, Umleitung per Namensauflösung und OpenSSH-Konfiguration,
Password- und Keyboard-Interactive-Login, PBKDF2-Migration, KDF-Downgrade,
Mindestanforderungen und Fehlversuchs-Drosselung ab.
