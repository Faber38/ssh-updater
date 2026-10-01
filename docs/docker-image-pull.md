# Docker Image-Pull – kein Containerwechsel

Der Docker-Update-Button startet in einem abbrechbaren Worker:

1. einen neuen Live-Preflight des gesamten freigegebenen Plans;
2. sequenziell pro Kandidatenprojekt/Service eine erneute lokale Prüfung;
3. ausschließlich den gezielten Image-Pull;
4. Read-only-Kontrollen des Containerzustands und des lokal geladenen Images.

Bei fehlgeschlagenem Gesamt-Preflight wird kein Pull gestartet. Vor jedem Pull
werden Compose- und lokale Containeridentitäten nochmals geprüft. Die bereits
geprüfte Compose-Datei wird über dieselbe explizite Kommando-Grundlage wie bei
`config` angesprochen. Es wird keine eigene YAML-Auflösung eingeführt.

## Einziger verändernder Docker-Befehl

Für `EMPTY_CONTEXT` bleibt der bisherige Aufruf unverändert:

```text
env COMPOSE_PARALLEL_LIMIT=1 COMPOSE_PROFILES= COMPOSE_ENV_FILES= COMPOSE_DISABLE_ENV_FILE=1 docker compose --project-name <Projekt> --project-directory <Verzeichnis> --env-file /dev/null -f <Compose-Pfad> pull --policy always --quiet -- <freigegebener Service>
```

Für `PROJECT_DOTENV_CONTEXT` verwendet derselbe Pull die kontrollierte
`env -i`-Policy aus [Compose-Kontext](docker-image-updates.md), einschließlich der festen
Infrastruktur-Allowlist und COMPOSE-Neutralisierung, mit exakt dem im Plan
gebundenen `--env-file <Projektverzeichnis>/.env`. Projektname, Verzeichnis,
Compose-Datei und Serviceauswahl bleiben explizit. Keine `.env`-Werte werden
im Plan oder Pull-Ergebnis gespeichert. Eine beim erneuten Live-Preflight
festgestellte Config-/Kontextabweichung verhindert den Pull.

Pro Service ein Aufruf: Dadurch sind abgeschlossene und fehlgeschlagene Pulls
klar zuordenbar. Mehrere Replikate eines Service führen zu nur einem Pull.
Keine Abhängigkeiten werden durch `--include-deps` hinzugefügt; Fehler werden
nicht mit `--ignore-pull-failures` übergangen. Build-, gepinnte und nicht prüfbare
Projekte bleiben durch die konservative Planbildung ausgeschlossen. Es werden
nur im Plan belegte Update-Services angesprochen. Vorhandene Docker-Credentials verwendet Docker
selbst; SSH Updater führt keinen Login aus.

`--policy always` prüft denselben konfigurierten Tag auch bei vorhandenem
lokalem Image. Bereits vorhandene Layer können wiederverwendet werden; ein
Pull-Erfolg behauptet deshalb keine bestimmte neu heruntergeladene Datenmenge.
Ein Tag kann sich seit der Vorschau erneut geändert haben. Der tatsächlich
lokal geladene Descriptor wird gesondert festgehalten, ohne den ursprünglichen
Plan oder den Registry-RAM-Cache umzuschreiben. Der anschließende
[Apply](docker-apply.md) baut auf dieser konkreten Identität auf, nicht allein
auf dem Tag-Namen.

Quelle: [Docker Compose pull](https://docs.docker.com/reference/cli/docker/compose/pull/).

## Ergebnis und Abbruch

Exitcode 0 bestätigt den Pull. Anschließend werden Container-ID, Image-ID,
Name, Startzeit, Neustartzähler und Running-Zustand vor/nach dem Pull verglichen.
`docker image inspect <Referenz>` liefert die neue lokale Image-ID, Plattform
und gegebenenfalls den typisierten Descriptor. Diese Daten sind keine Aussage,
dass der laufende Container bereits aktualisiert wurde.

Ein Fehler stoppt alle weiteren Pulls. Erfolgreiche Teilschritte bleiben im
Ergebnis sichtbar und lokal geladen. Es gibt keinen Rollback. Bei unbestätigtem
Abschluss, Timeout oder Abbruch gilt: lokaler Imagebestand möglicherweise
teilweise verändert. Das bestehende SSH-Warten schließt den Kanal; dies beweist
keine Beendigung des Remote-Pulls. Keine automatische Wiederholung/Bereinigung.

Pull-Zeitlimit: 30 Minuten; die bestehende Remote-Schicht begrenzt zusätzlich
inaktive Wartezeiten auf fünf Minuten und Ausgaben auf 4 MiB. Keine Rohausgaben
oder Credential-Helper-Fehler gelangen in den Ergebnisdialog. Registryfehler
werden mit bestehenden festen Fehlertexten klassifiziert.

Nach jedem Lauf wird die Freigabe verbraucht. Die bisherige Prüfergebnisrevision
kann nicht durch Wiederöffnen der Vorschau erneut freigegeben werden.
`_docker_pull_state` hält ausschließlich im RAM den ursprünglichen Plan und
strukturierte Ergebnisse. `apply_pending` im Pull-Bericht zeigt an, dass
mindestens ein Pull bestätigt wurde; das allein reicht nicht zur Apply-Freigabe.
Nur Gesamtstatus `pulled`, bestätigte Exitcodes und die exakte vollständige
Abdeckung der vorbereiteten Services erlauben „Docker anwenden“. Fehler,
Abbruch und Teilerfolg ergeben keine pauschale Apply-Freigabe.

Bei vollständig vorbereitetem Zustand wechselt derselbe Toolbar-Button zu
„Docker anwenden“. Die Update-Vorschau bleibt währenddessen gesperrt.
Checkboxänderungen verändern die vorbereiteten Ziele nicht. Der alte
Freigabeplan ist verbraucht; Apply verwendet ausschließlich diesen Pull-Zustand
und führt zuvor einen neuen Live-Precheck aus. Ein Programmneustart übernimmt
keinen Zustand und rekonstruiert ihn nicht aus lokalen Tags.

## Grenzen

Kein `up`, `down`, Restart, Stop/Start, `rm`, `rmi`, Build, Push oder Prune.
Keine Compose-Dateiänderung. Kein DB-/Dateispeicher für den Zwischenzustand.
Keine Registry-Cache-Manipulation. Der Plan wird nicht stillschweigend angepasst.
