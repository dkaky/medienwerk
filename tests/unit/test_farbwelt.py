"""Die alte, fremde Farbwelt darf nicht zurueckkommen.

Das Design der Oberflaeche war bis zuletzt ein fremdes - Flaschengruen,
Messing, Elfenbein, Serifenschrift. Der Code sagte es selbst: im Stil-Block
standen noch die Namen der fremden Palette. Die Embleme (Wappen, Siegel MMXXIV,
Motto, Raketenbanner) waren laengst entfernt, die FARBEN nicht.

Dieser Test ist eine Wache, kein Geschmacksurteil. Er haelt drei Dinge fest:

1. Die alten Markenfarben stehen nirgends mehr. Sie kaemen sonst still zurueck -
   beim naechsten Uebernehmen aus einer alten Vorlage, oder weil jemand einen
   Farbwert aus einem alten Bildschirmfoto abliest.
2. Die Anmeldeseite traegt DIESELBE Palette wie das Dashboard. Sie steckt in
   Python statt in der HTML-Datei und wird deshalb beim Umfaerben leicht
   vergessen - sie ist aber das Erste, was man sieht.
3. Jede Textfarbe bleibt lesbar. Zwei Farben lagen beim ersten Anlauf unter dem
   noetigen Kontrast: huebsch und unlesbar.
"""

from __future__ import annotations

import re
from pathlib import Path

WURZEL = Path(__file__).resolve().parents[2]
SEITE = WURZEL / "app" / "static" / "index.html"
ANMELDUNG = WURZEL / "app" / "auth.py"
FAVICON = WURZEL / "app" / "static" / "favicon.svg"
STUDIO = WURZEL / "app" / "static" / "studio.html"

# Die tragenden Farben der alten Palette. Bewusst nur die eindeutigen - ein
# neutrales Grau wuerde Fehlalarme ausloesen.
ALTE_MARKENFARBEN = [
    "#5e9a6f",   # Akzent, Flaschengruen
    "#c8a765",   # Messing
    "#a5843f",   # Messing dunkel
    "#2c5140",   # Waldgruen (helles Schema)
    "#a6813f",   # Messing (helles Schema)
    "#0e1a15",   # Grund dunkel
    "#f6f2e9",   # Elfenbein
    "#ece3cf",   # Creme-Text
]

CYAN_DUNKEL = "#19a7bd"
CYAN_HELL = "#0e7f92"

# Das Studio kam aus einem eigenen Entwurf und trug ein Violett. Es ist
# dieselbe Anwendung - zwei Seiten duerfen nicht aussehen wie zwei Programme.
STUDIO_VIOLETT = ["#6c5ce7", "#8b7bf0"]


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8").lower()


def test_dashboard_traegt_keine_alte_farbe_mehr():
    gefunden = [f for f in ALTE_MARKENFARBEN if f in _text(SEITE)]
    assert not gefunden, f"Alte Markenfarben zurueck in index.html: {gefunden}"


def test_anmeldeseite_traegt_keine_alte_farbe_mehr():
    """Sie steckt in Python und wird beim Umfaerben leicht uebersehen."""
    gefunden = [f for f in ALTE_MARKENFARBEN if f in _text(ANMELDUNG)]
    assert not gefunden, f"Alte Markenfarben zurueck in auth.py: {gefunden}"


def test_anmeldeseite_und_dashboard_teilen_die_markenfarbe():
    """Ein Bruch dazwischen sieht aus wie zwei verschiedene Programme."""
    assert CYAN_DUNKEL in _text(SEITE)
    assert CYAN_DUNKEL in _text(ANMELDUNG)


def test_favicon_traegt_die_markenfarbe():
    q = _text(FAVICON)
    assert CYAN_DUNKEL in q, "Das Zeichen im Browser-Reiter gehoert zur Marke"
    assert "#c8a765" not in q, "Das alte Messing ist zurueck"


def test_beide_schemata_existieren_weiter():
    """Das Studio kann nur dunkel - der Schalter darf beim Uebernehmen nicht wegfallen."""
    q = SEITE.read_text(encoding="utf-8")
    assert "body.light" in q, "Das helle Schema fehlt"
    assert CYAN_HELL in q.lower(), "Das helle Schema hat keinen eigenen Akzent"
    assert CYAN_DUNKEL != CYAN_HELL


def test_keine_serifenschrift_mehr_im_einsatz():
    """Die Variable --serif bleibt (sechs Fundstellen), traegt aber Systemschrift.

    Geprueft wird deshalb ihr WERT, nicht ihr Name.
    """
    q = SEITE.read_text(encoding="utf-8")
    zeile = next(z for z in q.split("\n") if z.strip().startswith("--serif:"))
    for schrift in ("Hoefler", "Baskerville", "Georgia", "Times New Roman"):
        assert schrift not in zeile, f"{schrift} ist zurueck"


def _kontrast(a: str, b: str) -> float:
    def leucht(h: str) -> float:
        h = h.lstrip("#")
        w = []
        for i in (0, 2, 4):
            c = int(h[i:i + 2], 16) / 255
            w.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * w[0] + 0.7152 * w[1] + 0.0722 * w[2]
    la, lb = leucht(a), leucht(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


FAELLE = [
    ("Text dunkel", "#e6e9ef", "#1f2530"),
    ("Gedaempft dunkel", "#8b93a3", "#1f2530"),
    ("Akzent dunkel", "#19a7bd", "#1f2530"),
    ("Warnung dunkel", "#f2a63b", "#1f2530"),
    ("Fehler dunkel", "#ff5c6c", "#1f2530"),
    ("Text hell", "#1a1f26", "#ffffff"),
    ("Gedaempft hell", "#66707d", "#ffffff"),
    ("Akzent hell", "#0e7f92", "#ffffff"),
    ("Warnung hell", "#9f6316", "#ffffff"),
    ("Gut hell", "#1d814d", "#ffffff"),
]


def test_kontraste_reichen_zum_lesen():
    """Mindestens 4.5:1 (WCAG AA) fuer normalen Text.

    Beim ersten Anlauf lagen Warnung und Gut im hellen Schema bei 3.9 und 4.4.
    Dieser Test verhindert, dass beim naechsten Nachjustieren wieder nach
    Augenmass gewaehlt wird.
    """
    schwach = [(n, round(_kontrast(v, h), 2)) for n, v, h in FAELLE
               if _kontrast(v, h) < 4.5]
    assert not schwach, f"Zu schwacher Kontrast: {schwach}"


def test_die_geprueften_farben_stehen_wirklich_in_der_datei():
    """Gegenprobe gegen einen Test, der nur sich selbst prueft.

    Ohne sie bliebe der Kontrast-Test gruen, waehrend die Oberflaeche laengst
    andere Farben benutzt.
    """
    q = SEITE.read_text(encoding="utf-8")
    for _, vordergrund, _ in FAELLE:
        assert re.search(r"--[a-z0-9-]+:\s*" + vordergrund, q, re.I), \
            f"{vordergrund} steht in keiner Variablen - der Kontrast-Test prueft Fantasie"

# ---------------------------------------------------------------- Studio
def test_studio_traegt_dieselbe_markenfarbe():
    """Dashboard und Studio sind eine Anwendung, keine zwei."""
    q = _text(STUDIO)
    assert CYAN_DUNKEL in q, "Das Studio kennt die Markenfarbe nicht"


def test_studio_hat_das_violett_abgelegt():
    """Der alte Akzent stammte aus einem Entwurf, nicht aus einem Markenbild.

    Der Wert darf im erklaerenden Kommentar stehen, aber in keiner Regel.
    """
    zeilen = STUDIO.read_text(encoding="utf-8").splitlines()
    treffer = [z.strip()[:70] for z in zeilen
               if any(v in z.lower() for v in STUDIO_VIOLETT)
               and not z.strip().startswith(("/*", "*", "//"))
               and "Vorher stand hier" not in z]
    assert not treffer, f"Violett noch in einer Regel: {treffer}"


def test_studio_kann_auch_hell():
    """Fehlte bisher. Wer im Dashboard auf Hell stellte, bekam hier einen Bruch."""
    q = STUDIO.read_text(encoding="utf-8")
    assert "body.light" in q, "Das Studio hat kein helles Schema"
    assert CYAN_HELL in q.lower(), "Das helle Schema hat keinen eigenen Akzent"


def test_studio_uebernimmt_die_wahl_des_dashboards():
    """Derselbe Speicherschluessel - sonst braeuchte es einen zweiten Schalter."""
    q = STUDIO.read_text(encoding="utf-8")
    assert "podshop-farbschema" in q,         "Das Studio liest die Schema-Wahl nicht"
    # Und zwar VOR dem Zeichnen, sonst blitzt das falsche Design auf.
    assert q.index("podshop-farbschema") < q.index('class="app"'),         "Die Schema-Wahl muss vor dem Inhalt stehen"


def test_studio_kontraste_reichen_zum_lesen():
    faelle = [
        ("Text dunkel", "#e6e9ef", "#1f2530"),
        ("Gedaempft dunkel", "#8b93a3", "#1f2530"),
        ("Akzent dunkel", "#19a7bd", "#1f2530"),
        ("Text auf Knopf dunkel", "#04171b", "#19a7bd"),
        ("Text hell", "#1a1f26", "#ffffff"),
        ("Gedaempft hell", "#66707d", "#ffffff"),
        ("Akzent hell", "#0e7f92", "#ffffff"),
        ("Text auf Knopf hell", "#ffffff", "#0e7f92"),
    ]
    schwach = [(n, round(_kontrast(v, h), 2)) for n, v, h in faelle
               if _kontrast(v, h) < 4.5]
    assert not schwach, f"Zu schwacher Kontrast im Studio: {schwach}"
