#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "Prüfe Python-Abhängigkeiten ..."
source .venv/bin/activate

# Wichtig: Paketpfad bekannt machen
export PYTHONPATH="src"

echo "Starte SSH-Updater ..."
python -m sshupdater.app
