#!/usr/bin/env python3
"""
Hochschulsport-Buchungs-Watcher (Phase A).

Beobachtet eine IbuSYS/BuchSys-Kurskategorie-Seite
(anmeldung.sport.uni-erlangen.de) und meldet, sobald ein konfigurierter
Zielkurs buchbar wird (Standard-Modus: nur lesende GET-Requests, es wird
NICHTS automatisch gebucht).

Sobald der konfigurierte Kurs buchbar wird, fuehrt es den echten Buchungs-
/Login-Flow (buchen -> Login -> Datenpruefung -> finale Bestaetigung) sofort
automatisch aus - ein Start des Skripts reicht von der Beobachtung bis zur
abgeschlossenen Buchung. Alle Parameter (Zugangsdaten, Zielkurs, Oeffnungs-
zeit) kommen aus der .env-Datei.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

USER_AGENT = "Mozilla/5.0 (hsp-watcher; +https://github.com/)"
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}
BOOKING_ENDPOINT = "https://www.anmeldung.sport.uni-erlangen.de/hsp/cgi/anmeldung.fcgi"
TIMEZONE = ZoneInfo("Europe/Berlin")
REQUEST_TIMEOUT = 10


def fetch_category_page(url: str) -> str:
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


def parse_courses(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    bs_code_input = soup.find("input", attrs={"name": "BS_Code"})
    bs_code = bs_code_input["value"] if bs_code_input else None

    courses = []
    for row in soup.find_all("tr", class_=("bs_odd", "bs_even")):
        kursnr_span = row.find("td", class_="bs_sknr")
        kursnr = kursnr_span.get_text(strip=True) if kursnr_span else None

        sdet_cell = row.find("td", class_="bs_sdet")
        title = None
        if sdet_cell:
            outer_span = sdet_cell.find("span")
            title = outer_span.get_text(" ", strip=True) if outer_span else sdet_cell.get_text(" ", strip=True)

        schedule = []
        stzo_cell = row.find("td", class_="bs_stzo")
        if stzo_cell:
            for tzo_row in stzo_cell.find_all("tr"):
                day_cell = tzo_row.find("td", class_="bs_stag")
                time_cell = tzo_row.find("td", class_="bs_szeit")
                loc_cell = tzo_row.find("td", class_="bs_sort")
                schedule.append(
                    {
                        "weekday": day_cell.get_text(strip=True) if day_cell else "",
                        "time": time_cell.get_text(strip=True) if time_cell else "",
                        "location": loc_cell.get_text(strip=True) if loc_cell else "",
                    }
                )

        price_cell = row.find("td", class_="bs_spreis")
        price = price_cell.find("span").get_text(strip=True) if price_cell and price_cell.find("span") else None

        buch_cell = row.find("td", class_="bs_sbuch")
        state = "unbekannt"
        kursid_field = None
        opens_at_hint = None
        button = buch_cell.find("input", class_="bs_btn_buchen") if buch_cell else None
        autostart = buch_cell.find(class_="bs_btn_autostart") if buch_cell else None
        if button:
            state = "buchbar"
            kursid_field = button.get("name")
        elif buch_cell and buch_cell.find("input", class_="bs_btn_warteliste"):
            state = "warteliste"
            kursid_field = buch_cell.find("input", class_="bs_btn_warteliste").get("name")
        elif autostart:
            state = "noch_nicht_offen"
            opens_at_hint = autostart.get_text(strip=True)
        elif buch_cell and buch_cell.find(class_="bs_btn_gesperrt"):
            state = "gesperrt"

        courses.append(
            {
                "kursnr": kursnr,
                "title": title,
                "schedule": schedule,
                "price": price,
                "state": state,
                "kursid_field": kursid_field,
                "bs_code": bs_code,
                "opens_at_hint": opens_at_hint,
            }
        )
    return courses


def find_by_kursnr(courses: list[dict], kursnr: str) -> dict | None:
    kursnr = str(kursnr).strip()
    for course in courses:
        if course["kursnr"] == kursnr:
            return course
    return None


def print_listing(courses: list[dict]) -> None:
    if not courses:
        print("Keine Kurse auf dieser Seite gefunden.")
        return
    for course in courses:
        print(f"- {course['title']}  [{course['kursnr']}]  Status: {course['state']}")
        for entry in course["schedule"]:
            print(f"    {entry['weekday']} {entry['time']}  {entry['location']}")
        print(f"    Preis: {course['price']}   Formularfeld: {course['kursid_field']}")
        if course.get("opens_at_hint"):
            print(f"    Öffnet laut Seite: {course['opens_at_hint']}")


OPENING_TIME_FORMATS = ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M")


def parse_opening_time(raw: str) -> datetime:
    """Parst ein menschenlesbares Datum/Uhrzeit, z.B. '01.10.2026 08:00:00' (Zeitzone Europe/Berlin)."""
    raw = raw.strip()
    for fmt in OPENING_TIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=TIMEZONE)
        except ValueError:
            continue
    raise ValueError(
        f"Kann HSP_OPENING_TIME='{raw}' nicht lesen. Erwartetes Format: TT.MM.JJJJ HH:MM[:SS], "
        f"z.B. 01.10.2026 08:00:00"
    )


def wait_until_window_start(opening_dt: datetime) -> None:
    window_start = opening_dt - timedelta(seconds=3)
    now = datetime.now(TIMEZONE)
    sleep_seconds = (window_start - now).total_seconds()
    if sleep_seconds > 0:
        print(f"Warte bis {window_start.isoformat()} (noch {sleep_seconds:.0f}s)...")
        time.sleep(sleep_seconds)


def poll_loop(
    category_url: str,
    kursnr: str,
    opening_dt: datetime,
    email: str,
    password: str,
    iban: str | None = None,
    bic: str | None = None,
    kontoinhaber: str | None = None,
) -> bool:
    window_start = opening_dt - timedelta(seconds=3)
    window_end = opening_dt + timedelta(seconds=10)

    tick = window_start
    while tick <= window_end:
        now = datetime.now(TIMEZONE)
        sleep_seconds = (tick - now).total_seconds()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

        timestamp = datetime.now(TIMEZONE).strftime("%H:%M:%S")
        try:
            html = fetch_category_page(category_url)
            course = find_by_kursnr(parse_courses(html), kursnr)
        except requests.RequestException as exc:
            print(f"[{timestamp}] Fehler beim Abrufen der Seite: {exc}")
            tick += timedelta(seconds=1)
            continue

        if course is None:
            print(f"[{timestamp}] Zielkurs nicht gefunden.")
        else:
            print(f"[{timestamp}] Status: {course['state']}")
            if course["state"] == "buchbar":
                print("=" * 60)
                print("KURS IST JETZT BUCHBAR! Starte automatische Buchung...")
                print(f"Titel: {course['title']}")
                print("=" * 60)
                try:
                    result_html, result_url = run_booking_flow(category_url, kursnr, email, password, iban, bic, kontoinhaber)
                except (RuntimeError, requests.RequestException) as exc:
                    print(f"BUCHUNG FEHLGESCHLAGEN: {exc}")
                    return False
                report_booking_result(result_html, result_url, kursnr)
                return True

        tick += timedelta(seconds=1)

    print("Zeitfenster abgelaufen, Kurs wurde nicht als buchbar erkannt.")
    return False


def start_booking(session: requests.Session, category_url: str, kursnr: str) -> tuple[str, str]:
    """Klickt 'buchen' fuer den Kurs mit der gegebenen Kursnr. Gibt (html, url) der Antwortseite zurueck."""
    r = session.get(category_url, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    course = find_by_kursnr(parse_courses(r.text), kursnr)
    if course is None:
        raise RuntimeError(f"Kurs {kursnr} nicht gefunden auf {category_url}")
    if course["state"] != "buchbar":
        raise RuntimeError(f"Kurs {kursnr} ist aktuell nicht buchbar (Status: {course['state']})")

    data = {"BS_Code": course["bs_code"], course["kursid_field"]: "buchen"}
    headers = dict(BROWSER_HEADERS, Referer=category_url, Origin="https://www.anmeldung.sport.uni-erlangen.de")
    r2 = session.post(BOOKING_ENDPOINT, data=data, headers=headers, timeout=REQUEST_TIMEOUT)
    r2.raise_for_status()
    return r2.text, r2.url


def parse_login_form(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", attrs={"name": "bsform"})
    if form is None:
        raise RuntimeError("Erwartetes Buchungsformular (bsform) nicht gefunden - Seitenstruktur weicht ab.")

    fid_input = form.find("input", attrs={"name": "fid"})
    pwd_input = form.find("input", attrs={"type": "password"})
    checkbox = form.find("input", attrs={"name": "tnbed"})
    if fid_input is None or pwd_input is None or checkbox is None:
        raise RuntimeError("Login-/AGB-Felder nicht im Formular gefunden - Seitenstruktur weicht ab.")

    return {
        "fid": fid_input["value"],
        "pw_pwd_field": pwd_input["name"],
        "tnbed_value": checkbox.get("value", "1"),
    }


def submit_login(session: requests.Session, referer_url: str, login_info: dict, email: str, password: str) -> requests.Response:
    data = {
        "fid": login_info["fid"],
        "pw_email": email,
        login_info["pw_pwd_field"]: password,
        "tnbed": login_info["tnbed_value"],
    }
    headers = dict(BROWSER_HEADERS, Referer=referer_url, Origin="https://www.anmeldung.sport.uni-erlangen.de")
    r = session.post(BOOKING_ENDPOINT, data=data, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r


def extract_form_fields(form) -> list[tuple[str, str]]:
    """Liest den aktuellen Zustand eines Formulars aus, so wie ein Browser ihn mitsenden wuerde."""
    fields: list[tuple[str, str]] = []
    for inp in form.find_all("input"):
        itype = (inp.get("type") or "text").lower()
        name = inp.get("name")
        if not name:
            continue
        if itype in ("checkbox", "radio"):
            if inp.has_attr("checked"):
                fields.append((name, inp.get("value", "on")))
        elif itype in ("submit", "reset", "button", "image"):
            continue
        else:
            fields.append((name, inp.get("value", "")))
    for select in form.find_all("select"):
        name = select.get("name")
        if not name:
            continue
        chosen = select.find("option", selected=True) or select.find("option")
        fields.append((name, chosen.get("value", "") if chosen else ""))
    for textarea in form.find_all("textarea"):
        name = textarea.get("name")
        if name:
            fields.append((name, textarea.get_text()))
    return fields


def set_field(fields: list[tuple[str, str]], name: str, value: str) -> list[tuple[str, str]]:
    return [f for f in fields if f[0] != name] + [(name, value)]


def apply_payment_overrides(form, fields: list[tuple[str, str]], overrides: dict) -> list[tuple[str, str]]:
    """Ueberschreibt IBAN/BIC/Kontoinhaber nur, wenn ein Wert gesetzt ist UND
    das Feld in diesem Schritt noch editierbar (nicht hidden) ist - analog zum
    tnbed-Handling, um die Pruefsumme spaeterer Schritte nicht zu brechen.
    Ohne gesetzten Override bleibt der aus dem Account vorausgefuellte Wert
    (falls vorhanden) unveraendert erhalten.
    """
    for name, value in overrides.items():
        if not value:
            continue
        inp = form.find("input", attrs={"name": name})
        if inp is not None and (inp.get("type") or "text").lower() != "hidden":
            fields = set_field(fields, name, value)
    return fields


def run_booking_flow(
    category_url: str,
    kursnr: str,
    email: str,
    password: str,
    iban: str | None = None,
    bic: str | None = None,
    kontoinhaber: str | None = None,
    max_confirm_steps: int = 4,
) -> tuple[str, str]:
    """Fuehrt buchen -> Login -> (ggf. mehrere) Bestaetigungsschritte aus.

    Nach dem Login liefert die Seite ein mit den Account-Daten vorausgefuelltes
    Formular zurueck, das noch einmal (mit gesetztem AGB-Haken) abgesendet
    werden muss. Dieser Schritt wird generisch wiederholt, bis entweder kein
    bsform mehr zurueckkommt (Endzustand) oder das Login-Feld erneut auftaucht
    (z.B. falsches Passwort) oder das Sicherheits-Limit erreicht ist.
    """
    session = requests.Session()
    html, url = start_booking(session, category_url, kursnr)

    login_info = parse_login_form(html)
    response = submit_login(session, url, login_info, email, password)

    for _ in range(max_confirm_steps):
        soup = BeautifulSoup(response.text, "html.parser")
        if soup.find(class_="bs_meldung"):
            # Terminale Fehlerseite (z.B. "bereits gebucht") - nicht weiter
            # resubmitten, sonst wird der Vorgang vom Server zurueckgesetzt.
            break
        form = soup.find("form", attrs={"name": "bsform"})
        if form is None:
            break
        if form.find("input", attrs={"type": "password"}):
            break

        fields = extract_form_fields(form)
        fields = apply_payment_overrides(form, fields, {"iban": iban, "bic": bic, "kontoinh": kontoinhaber})
        checkbox = form.find("input", attrs={"name": "tnbed", "type": "checkbox"})
        if checkbox is not None:
            # AGB noch nicht bestaetigt (Checkbox-Schritt) - jetzt setzen.
            fields = set_field(fields, "tnbed", checkbox.get("value", "1"))
        # Ist tnbed schon ein hidden Feld (spaetere Schritte), unveraendert
        # uebernehmen - ueberschreiben wuerde die serverseitige Pruefsumme
        # (_formdata) ungueltig machen und den Vorgang zuruecksetzen.
        headers = dict(BROWSER_HEADERS, Referer=response.url, Origin="https://www.anmeldung.sport.uni-erlangen.de")
        response = session.post(BOOKING_ENDPOINT, data=fields, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

    return response.text, response.url


def find_confirmation_link(html: str, base_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True).lower()
        if any(keyword in text for keyword in ("bestätig", "confirmation", "pdf", "drucken", "print")):
            return urljoin(base_url, a["href"])
    return None


def summarize_result(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else None
    messages = []

    # bs_meldung-Divs sind verschachtelt (aeusseres enthaelt zusaetzlichen
    # Kontext) - nur das innerste (spezifischste) je Fundstelle nehmen.
    meldungen = soup.find_all(class_="bs_meldung")
    for el in meldungen:
        if not el.find(class_="bs_meldung"):
            text = el.get_text(" ", strip=True)
            if text:
                messages.append(text)

    for el in soup.find_all(class_=("bs_text_red", "bs_error", "bs_form_error")):
        text = el.get_text(" ", strip=True)
        if text and not any(text in m or m in text for m in messages):
            messages.append(text)

    lines = []
    if title:
        lines.append(f"Seitentitel: {title}")
    for msg in dict.fromkeys(messages):
        lines.append(f"Hinweis/Fehler auf der Seite: {msg}")
    return "\n".join(lines) if lines else "(keine eindeutigen Status-Hinweise gefunden, siehe gespeicherte HTML-Datei)"


def report_booking_result(result_html: str, result_url: str, kursnr: str) -> bool:
    """Gibt das Ergebnis aus und liefert True nur bei erkanntem Erfolg zurueck."""
    soup = BeautifulSoup(result_html, "html.parser")
    success = soup.find(class_="bs_meldung") is None

    print("BUCHUNG ERFOLGREICH" if success else "BUCHUNG NICHT ERFOLGREICH")
    print(summarize_result(result_html))

    # Die Buchungsbestaetigung/Fehlerseite ist nur der Inhalt dieser letzten
    # Antwort - es gibt dafuer keine eigene, spaeter abrufbare URL auf der
    # Website (anders als beim manuellen Buchen im Browser mit target=_blank).
    # Deshalb lokal speichern und als klickbaren file://-Link ausgeben.
    confirmation_path = os.path.abspath(f"booking_confirmation_{kursnr}.html")
    with open(confirmation_path, "w", encoding="utf-8") as f:
        f.write(result_html)
    print(f"Antwort lokal gespeichert, zum Pruefen im Browser oeffnen: file://{confirmation_path}")

    link = find_confirmation_link(result_html, result_url)
    if link:
        print(f"Zusaetzlich auf der Seite verlinkt: {link}")

    return success


def run_login_test(
    category_url: str,
    kursnr: str,
    email: str,
    password: str,
    iban: str | None = None,
    bic: str | None = None,
    kontoinhaber: str | None = None,
) -> int:
    print(f"Starte Buchungsvorgang fuer Kurs {kursnr} ({category_url})...")
    try:
        result_html, result_url = run_booking_flow(category_url, kursnr, email, password, iban, bic, kontoinhaber)
    except (RuntimeError, requests.RequestException) as exc:
        print(f"BUCHUNG FEHLGESCHLAGEN: {exc}")
        return 1
    return 0 if report_booking_result(result_html, result_url, kursnr) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Hochschulsport Buchungs-Watcher")
    parser.add_argument("--list", action="store_true", help="Nur aktuelle Kurse auf der Kategorie-Seite auflisten")
    parser.add_argument("--url", help="Kategorie-URL (ueberschreibt HSP_CATEGORY_URL aus der .env)")
    parser.add_argument("--kursnr", help="Kursnummer (ueberschreibt HSP_KURSNR aus der .env)")
    parser.add_argument(
        "--login-test",
        action="store_true",
        help="Fuehrt den echten Buchungs-/Login-Flow sofort aus, ohne auf die Oeffnungszeit zu warten.",
    )
    parser.add_argument(
        "--profile",
        help=(
            "Pfad zu einer zusaetzlichen env-Datei mit HSP_CATEGORY_URL/HSP_KURSNR/"
            "HSP_OPENING_TIME fuer einen bestimmten Kurs (z.B. courses/laufen.env). "
            "Zugangsdaten bleiben in der normalen .env."
        ),
    )
    args = parser.parse_args()

    load_dotenv()
    if args.profile:
        load_dotenv(args.profile, override=True)
    category_url = args.url or os.environ.get("HSP_CATEGORY_URL")
    kursnr = args.kursnr or os.environ.get("HSP_KURSNR")

    if args.list:
        if not category_url:
            print("Keine Kategorie-URL angegeben (--url oder HSP_CATEGORY_URL in .env).")
            return 1
        html = fetch_category_page(category_url)
        print_listing(parse_courses(html))
        return 0

    email = os.environ.get("HSP_USERNAME")
    password = os.environ.get("HSP_PASSWORD")
    missing = [
        name
        for name, value in (
            ("HSP_CATEGORY_URL", category_url),
            ("HSP_KURSNR", kursnr),
            ("HSP_USERNAME", email),
            ("HSP_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        print(f"Fehlende Angaben (siehe .env): {', '.join(missing)}")
        return 1

    # Optional - nur fuer bezahlpflichtige Kurse, und nur falls im Account noch
    # kein Konto hinterlegt ist oder ein anderes genutzt werden soll. Ohne
    # diese Angaben wird ein bereits im Account gespeichertes Konto verwendet.
    iban = os.environ.get("HSP_IBAN") or None
    bic = os.environ.get("HSP_BIC") or None
    kontoinhaber = os.environ.get("HSP_KONTOINHABER") or None

    if args.login_test:
        return run_login_test(category_url, kursnr, email, password, iban, bic, kontoinhaber)

    opening_time_raw = os.environ.get("HSP_OPENING_TIME")
    if not opening_time_raw:
        print("HSP_OPENING_TIME nicht gesetzt (siehe .env).")
        return 1
    opening_dt = parse_opening_time(opening_time_raw)

    wait_until_window_start(opening_dt)
    success = poll_loop(category_url, kursnr, opening_dt, email, password, iban, bic, kontoinhaber)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
