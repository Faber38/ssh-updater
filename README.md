# SSH Updater

Ein **Qt/PyQt6-Tool**, um mehrere Linux-Hosts (z. B. Debian- oder Proxmox-Server) per SSH zu prüfen, zu simulieren und Updates auszuführen.

## ✨ Features
- Hostliste mit Filterfunktion  
- Aktionen: **Prüfen**, **Simulieren**, **Upgrade**  
- Import von Proxmox-Hosts  
- Konfigurationsdialog für Einstellungen  
- Übersichtliche Qt-Oberfläche mit Tabelle  

## 🚀 Quickstart (Entwicklung)
```bash
# Virtuelle Umgebung anlegen und aktivieren
python3 -m venv .venv
source .venv/bin/activate

# Abhängigkeiten installieren (falls requirements.txt vorhanden ist)
pip install -r requirements.txt

# Start
python -m src.sshupdater.ui_main
```

## 📌 Roadmap
- Log- und Dry-Run-Ansicht verbessern  
- Parallel-Executor für mehrere Hosts gleichzeitig  
- Erweiterte Host-Tags und Filter  

## 📄 Lizenz
MIT License – siehe [LICENSE](LICENSE)
