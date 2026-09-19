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

DESIGN = WURZEL / "app" / "static" / "design.css"

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


def test_stylesheet_traegt_keine_alte_farbe_mehr():
    gefunden = [f for f in ALTE_MARKENFARBEN if f in _text(DESIGN)]
    assert not gefunden, f"Alte Markenfarben zurueck in design.css: {gefunden}"


def test_alle_seiten_teilen_ein_stylesheet():
    """Dashboard, Studio und Anmeldeseite sind eine Anwendung, kein Nebeneinander."""
    for name, quelle in (("index.html", SEITE), ("studio.html", STUDIO), ("auth.py", ANMELDUNG)):
        assert 'href="/design.css' in quelle.read_text(encoding="utf-8"), \
            f"{name} bindet das gemeinsame Stylesheet nicht ein"


def test_beide_schemata_existieren():
    """Hell ist der Standard, Dunkel bleibt waehlbar."""
    q = DESIGN.read_text(encoding="utf-8")
    assert "body.light" in q, "Das helle Schema fehlt"
    assert ":root {" in q, "Das dunkle Schema fehlt"


def test_keine_serifenschrift_im_einsatz():
    q = DESIGN.read_text(encoding="utf-8")
    zeile = next(z for z in q.split("\n") if z.strip().startswith("--font:"))
    for schrift in ("Hoefler", "Baskerville", "Georgia", "Times New Roman", "serif,"):
        assert schrift not in zeile, f"{schrift} ist zurueck"


def test_akzent_ist_das_rot_des_logos_und_nirgends_das_alte_cyan():
    q = _text(DESIGN)
    assert "--brand:" in q
    for cyan in ("#19a7bd", "#0e7f92", "#3dc4d8"):
        assert cyan not in q, f"Das alte Cyan {cyan} ist zurueck"


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


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", block))


def _schemata() -> dict[str, dict[str, str]]:
    css = DESIGN.read_text(encoding="utf-8")
    dunkel = re.search(r":root\s*\{(.*?)\}", css, re.S).group(1)
    hell = re.search(r"body\.light\s*\{(.*?)\}", css, re.S).group(1)
    return {"dunkel": _tokens(dunkel), "hell": _tokens(hell)}


def test_kontraste_reichen_zum_lesen():
    """Mindestens 4.5:1 (WCAG AA) - gerechnet an den WERTEN in design.css.

    Der Test liest die Farben aus der Datei statt aus einer Liste im Test: eine
    Liste im Test bliebe gruen, waehrend die Oberflaeche laengst andere Farben
    benutzt.
    """
    schwach = []
    for schema, t in _schemata().items():
        for vorder, grund in (("ink", "card"), ("muted", "card"), ("ink", "bg"), ("muted", "bg"),
                              ("ok", "card"), ("warn", "card"), ("bad", "card"),
                              ("accent-ink", "accent")):
            k = _kontrast(t[vorder], t[grund])
            if k < 4.5:
                schwach.append((schema, vorder, grund, round(k, 2)))
    assert not schwach, f"Zu schwacher Kontrast: {schwach}"


# ---------------------------------------------------------------- Studio
def test_studio_hat_das_violett_abgelegt():
    """Der alte Akzent stammte aus einem Entwurf, nicht aus einem Markenbild."""
    zeilen = (STUDIO.read_text(encoding="utf-8") + DESIGN.read_text(encoding="utf-8")).splitlines()
    treffer = [z.strip()[:70] for z in zeilen
               if any(v in z.lower() for v in STUDIO_VIOLETT)
               and not z.strip().startswith(("/*", "*", "//"))
               and "Vorher stand hier" not in z]
    assert not treffer, f"Violett noch in einer Regel: {treffer}"


def test_studio_uebernimmt_die_wahl_des_dashboards():
    """Derselbe Speicherschluessel - sonst braeuchte es einen zweiten Schalter."""
    q = STUDIO.read_text(encoding="utf-8")
    assert "podshop-farbschema" in q, "Das Studio liest die Schema-Wahl nicht"
    # Und zwar VOR dem Zeichnen, sonst blitzt das falsche Design auf.
    assert q.index("podshop-farbschema") < q.index('class="app"'), \
        "Die Schema-Wahl muss vor dem Inhalt stehen"


def test_favicon_traegt_die_markenfarbe():
    q = _text(FAVICON)
    assert "#d62b31" in q, "Das Zeichen im Browser-Reiter ist das rote Quadrat des Logos"
    assert "#c8a765" not in q and "#19a7bd" not in q, "Eine alte Farbe ist zurueck"
