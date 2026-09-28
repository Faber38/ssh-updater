# Docker Apply – vorbereitetes Image anwenden

Der Button „Docker anwenden“ verbraucht ausschließlich den vollständig
bestätigten `_docker_pull_state` der laufenden GUI-Sitzung. Checkboxen und neue
Tabellendaten bestimmen keine Apply-Ziele. Kein Zustand wird gespeichert oder
nach einem Neustart aus vorhandenen Images rekonstruiert.

## Live-Precheck

`core/docker_apply.py` verwendet die bestehende SSH-/Host-Key-Schicht und
`docker_preflight.verify_project` für dieselbe konservative Compose-Auflösung:
Dateiinhalt, aufgelöste Konfiguration, Projektverzeichnis, Service-/Image- und
Plattformbindungen sowie die alten Container-/Descriptoridentitäten müssen
weiterhin passen. Zusätzlich müssen die Container-Snapshots aus dem Pull
(ID, Image-ID, Name, Startzeit, Restart-Zähler, Running) übereinstimmen.

Das geladene Image wird sowohl über den konfigurierten Tag als auch über die
beim Pull bestätigte lokale Image-ID inspiziert. Beide müssen exakt die
festgehaltene ID, Plattform und den gegebenenfalls vorhandenen typisierten
Descriptor liefern. Der alte Preflight-Erfolg wird nicht wiederverwendet.
Die gemeinsame Compose-Kontextprüfung akzeptiert das Environment-Label fehlend,
leer oder exakt `/dev/null`. Damit wird der eigene bewusst leere
`--env-file /dev/null`-Kontext auch nach Container-Neuerstellung erkannt;
andere Environment-Dateien bleiben ausgeschlossen.

Vor jedem Projekt erfolgen ein neuer Precheck und die bestehende Prüfung des
Host-Verbindungskontexts. Abweichungen erlauben keinen Up-Aufruf für dieses
Projekt. Bei mehreren Projekten bleiben frühere erfolgreiche Applies bestehen;
weitere Applies stoppen beim ersten Fehler. Keine atomare Mehrhost-Transaktion.

## Einziger verändernder Apply-Befehl

```text
env COMPOSE_PARALLEL_LIMIT=1 COMPOSE_PROFILES= COMPOSE_ENV_FILES= COMPOSE_DISABLE_ENV_FILE=1 docker compose --project-name <Projekt> --project-directory <Verzeichnis> --env-file /dev/null -f <Compose-Pfad> up -d --no-deps --pull never --no-build -- <vorbereitete Services>
```

Die Services werden aus den bestätigten Pull-Ergebnissen und dem ursprünglichen
unveränderlichen Plan ermittelt; vollständige Übereinstimmung ist erforderlich.
`--no-deps` verhindert das Starten abhängiger Services, `--pull never` das
Nachladen von Images und `--no-build` einen Build. Ein nicht unterstütztes Flag
wird nicht durch einen weniger restriktiven Fallback ersetzt.

Quelle: [Docker Compose up](https://docs.docker.com/reference/cli/docker/compose/up/).

Kein erneuter Pull, Down, expliziter Restart/Stop/Start, rm/rmi, Prune,
Dateiänderung oder Rollback. Compose darf ausschließlich innerhalb des
gezielten Up-Schritts die ausgewählten Service-Container ersetzen/starten.

## Kontrolle und Zwischenzustand

Exitcode 0 allein reicht nicht aus. Read-only werden anschließend die
Compose-Zuordnung, Containeranzahl, Running-Zustand und die tatsächlich
verwendete Image-ID samt lokalen Metadaten und Plattform kontrolliert.
Neue Container-IDs sind erlaubt. Nicht vorbereitete Services müssen ihre
bisherigen Container-Snapshots behalten. Neue IDs, Image-/Manifestidentitäten,
Plattform, Startzeit und Restart-Zähler werden strukturiert im RAM festgehalten.

Erfolg bedeutet `apply_succeeded` und `verification_pending`, nicht „aktuell“.
Der vorbereitete Pull-Zustand wird schon vor dem Apply-Start verbraucht.
Nach vollständig erfolgreichem Apply ist „Docker prüfen“ aktiv und startet die
separate [read-only Abschlussverifikation](docker-verification.md). Eine
konkurrierende Vorschau bleibt bis zum Abschluss gesperrt. Apply selbst führt
keine Registry-Abfragen aus und verändert keine Registry-Cache-Daten.
Bei einem Teilerfolg bleiben erfolgreiche Projekte im Bericht sichtbar, der
Gesamtfehler eröffnet aber keinen normalen `verification_pending`-Erfolgsweg.

## Fehler, Abbruch und Grenzen

Der Worker blockiert die GUI nicht. Stopp bricht das lokale Warten ab. Nach
Beginn eines Up-Aufrufs kann der Remote-Vorgang trotzdem weiterlaufen.
Bei Fehler/Abbruch versucht die Anwendung eine auf zehn Sekunden begrenzte
read-only Containeraufnahme. Ein unbekannter oder fehlerhafter Abschluss
behauptet weder Erfolg noch unveränderte Container. Rohes stdout/stderr wird
nicht in GUI-Ergebnisse übernommen. Keine automatische Wiederholung.

Es gibt keine gemeinsame atomare Sperre gegen externe Docker-/Dateiänderungen
zwischen Precheck und Up. Solche Eingriffe sind während Apply zu vermeiden;
die Nachkontrolle erkennt abweichende verwendete Images als Fehler. Die Prüfung
ist keine Healthcheck-/Bereitschaftsgarantie: kontrolliert wird der unmittelbare
Docker-Running-Zustand. Bereits begonnene Änderungen werden nicht zurückgerollt.
