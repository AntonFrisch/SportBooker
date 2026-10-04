# HSP Buchungs-Watcher

Beobachtet eine Kurs-Kategorieseite von anmeldung.sport.uni-erlangen.de und
bucht den konfigurierten Kurs automatisch, sobald die Anmeldung öffnet.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Konfiguration

Alles wird in `.env` eingetragen (siehe Beispielwerte/Kommentare dort):

- `HSP_USERNAME` / `HSP_PASSWORD` – Zugangsdaten des per "Passwort setzen"-Kurs angelegten Accounts
- `HSP_CATEGORY_URL` – Kategorie-Seite mit dem Zielkurs (z.B. Schwimmen)
- `HSP_KURSNR` – Kursnummer (Spalte "Nr.") des Zielkurses, ändert sich pro Semester
- `HSP_OPENING_TIME` – Öffnungszeit im Format `TT.MM.JJJJ HH:MM[:SS]`, Zeitzone Europe/Berlin


## Nutzung

**Aktuelle Kurse auf der Zielseite anzeigen** (zum Finden der richtigen `HSP_KURSNR`
und zum Prüfen, ob/wann ein Kurs schon buchbar ist):

```bash
python3 hsp_watcher.py --list
```

**Sofort einmalig buchen** (ohne auf die Öffnungszeit zu warten – bucht wirklich,
nur für bewusst gewählte Kurse verwenden, z.B. zum Testen mit dem kostenlosen
Passwort-Kurs):

```bash
python3 hsp_watcher.py --login-test
```

**Normalbetrieb** – wartet bis 3s vor `HSP_OPENING_TIME`, pollt danach einmal pro
Sekunde bis 10s nach Öffnung und bucht automatisch, sobald der Kurs buchbar wird:

```bash
python3 hsp_watcher.py
```

`--url` und `--kursnr` überschreiben `HSP_CATEGORY_URL`/`HSP_KURSNR` einmalig,
ohne `.env` zu ändern.

## Nach einer Buchung

Die Bestätigung wird lokal als `booking_confirmation_<kursnr>.html` gespeichert;
das Skript gibt dafür einen klickbaren `file://`-Link aus.
