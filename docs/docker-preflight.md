# Docker Live-Preflight

Der freigegebene Button „Docker-Update“ startet einen frischen read-only
Preflight und erst bei Erfolg den [gezielten Pull](docker-image-pull.md).
`core/docker_preflight.py` selbst verändert weder Images noch Container und
fragt keine Registry ab. Sein Erfolg ist eine zeitgebundene Beobachtung, keine
dauerhafte Garantie. Der Pull-Worker prüft den gesamten Plan neu und wiederholt
die lokale Projektprüfung unmittelbar vor jedem Service-Pull.

## Belege und Vergleich

Die normale Prüfung liest die Compose-Datei bereits zweimal und löst deren
Konfiguration mit Compose auf. Aus diesen vorhandenen Daten werden zusätzlich
SHA256-Fingerabdrücke der Datei und der kanonischen JSON-Konfiguration sowie
Service/Image/platform-Zuordnungen im Sitzungsergebnis gehalten. Rohe Compose-
Inhalte (mögliche Secrets) werden nicht im Plan gespeichert. Befehle und
Image-Vergleich der normalen Prüfung bleiben unverändert.

Diese Belege ergänzen den unveränderlichen RAM-Freigabeplan. Ältere Sitzungsergebnisse
ohne Belege werden nicht nachträglich aus dem Live-Zustand vervollständigt:
Eine neue normale Prüfung und Vorschau sind erforderlich.

`core/docker_preflight.py` prüft gegen die erwarteten Identitäten:

- Host-Verbindungskontext, bestehende SSH-/Host-Key-/Vault-Prüfung;
- Compose-Pfad, Dateiinhalt und aufgelöste Konfiguration;
- Projekt-/Service-/Arbeitsverzeichnis-Labels und explizite Plattform;
- vollständige Zuordnung der zuvor geprüften laufenden Container im Projekt;
- Container-ID, Name, Image-Referenz, tatsächlich verwendete Image-ID,
  OS/Architektur/Variante und lokaler Manifest-Descriptor.

Auch unveränderte Services innerhalb eines Kandidatenprojekts werden geprüft.
Containerersatz mit neuer ID wird konservativ abgelehnt. Die Referenz-/Image-
Identität des Plans wird niemals an den Live-Zustand angepasst. Registry-Digests
im Plan werden weder aktualisiert noch erneut abgefragt.

## Read-only-Befehle

`verify_project()` unterstützt Standard-`.env`-Snapshots mit
expliziter Kontextidentität und erneuter effektiver Config-Auflösung. Details
und Prozesspolicy stehen in `docker-image-updates.md`. Diese Snapshots sind
bei erfüllten bisherigen Image-/Eligibility-Regeln auch Pull-/Apply-Kandidaten.
Vor jedem Pull und vor Apply wird der gebundene Kontext erneut verglichen;
eine wirksame `.env`-Änderung invalidiert den alten Plan.
Die folgenden Befehle beschreiben weiterhin `EMPTY_CONTEXT`.

Die sichere Auflösung und lokale Inspektion aus `docker_image_updates.Checker`
werden wiederverwendet:

- `cat -- <Compose-Pfad>` (zu Beginn und erneut am Ende);
- `docker container ls --quiet --no-trunc --filter label=com.docker.compose.project=<Projekt>`;
- `docker container inspect <IDs>`;
- `env COMPOSE_PROFILES= COMPOSE_ENV_FILES= COMPOSE_DISABLE_ENV_FILE=1 docker compose --project-name <Projekt> --project-directory <Verzeichnis> --env-file /dev/null -f <Pfad> config --format json --no-interpolate --no-env-resolution`;
- `docker image inspect <tatsächliche Container-Image-ID>`.

Bei `No such image` bleibt der streng validierte vorhandene
`ImageManifestDescriptor`-Fallback nutzbar. Es gibt keinen Buildx-Aufruf, keine
Registry-Abfrage und keinen Zugriff auf den Registry-Sitzungscache im Preflight.
Die Grenzen für konservativ unterstützte Compose-Kontexte bleiben bestehen.

## Vorschau und Freigabeplan

`docker_plan.py` hält die gesamte Vorschauauswahl und getrennt die verändernden
Kandidaten als unveränderliche Snapshots. Ein Projekt wird nur aufgenommen,
wenn alle Image-Ergebnisse entweder `current` oder eindeutig belegte
`update_available` sind und mindestens ein Update vorliegt. Ungeklärte,
gepinnte oder lokal gebaute Images schließen das betreffende Projekt aus.
Innerhalb zulässiger Projekte gelangen nur nachgewiesene Update-Services in
den verändernden Plan; aktuelle Services bleiben Prüfgrundlage.

Auswahl, Prüfergebnisrevision, Compose- und Hostidentität binden die Freigabe.
Änderungen der GUI-/Sessiongrundlage invalidieren sie; reine Darstellung und
Schließen der Vorschau nicht. Externe Hoständerungen werden nicht permanent
überwacht, sondern durch erneute Prüfungen und den Live-Preflight erkannt.

## Worker, Fehler und Lebensdauer

Ein abbrechbarer Qt-Worker verwendet die bestehende SSH-Verbindungsschicht.
Hosts/Projekte werden sequenziell abgearbeitet, pro Befehl gilt das bestehende
Zeitlimit (20 Sekunden), insgesamt 180 Sekunden für den Plan. Die GUI bleibt
reaktionsfähig. Andere Hostaktionen können nicht parallel über die Toolbar
angestoßen werden. Stopp und Fensterschließen brechen konservativ ab.

Ein Fehler, Abbruch oder eine zwischenzeitliche Planänderung verwirft den
Gesamtplan; es gibt keine teilweise Preflight-Freigabe. Die gescheiterten Prüfergebnisrevisionen
können nicht durch bloßes Wiederöffnen der Vorschau erneut freigegeben werden.
Neue normale Prüfung und neue Vorschau sind erforderlich. Fehlermeldungen sind
fest klassifiziert; SSH-/Credential-Helper-Rohmeldungen werden nicht angezeigt.

Der strukturierte Preflight-Bericht wird vom Pull-Worker ausgewertet. Bei
fehlgeschlagenem Gesamt-Preflight startet kein Pull; bei Erfolg folgt der Pull
im selben Arbeitsablauf. Scheitert eine erneute Prüfung vor einem späteren
Service-Pull, bleiben bereits geladene Images erhalten, weitere Pulls stoppen.
Die GUI zeigt das zusammengefasste Preflight-/Pull-Ergebnis. Die Aussage
„keine Änderungen“ gilt nur, solange noch kein Pull versucht wurde.

Keine Datei-/Datenbankpersistenz für Plan oder Preflight-Ergebnis. Ein Erfolg
wird nicht als dauerhafte Freigabe gespeichert oder für einen neuen Pull-Lauf
wiederverwendet. Stopp kann das lokale Warten abbrechen, garantiert aber nicht
die Beendigung eines bereits gestarteten Remote-Prozesses.
