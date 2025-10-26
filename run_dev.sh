#!/bin/bash
# -------------------------------------
# SSH-Updater – Entwicklungsstartskript
# -------------------------------------

# Virtuelle Umgebung erstellen (falls noch nicht vorhanden)
if [ ! -d ".venv" ]; then
  echo "Erstelle virtuelle Umgebung ..."
  python3 -m venv .venv
fi

# Aktivieren
source .venv/bin/activate

# Abhängigkeiten prüfen (nur bei Bedarf)
if [ -f "requirements.txt" ]; then
  echo "Prüfe Python-Abhängigkeiten ..."
  pip install -r requirements.txt --quiet --upgrade-strategy only-if-needed
fi

# Start der App
echo "Starte SSH-Updater ..."
python -m src.sshupdater.app
