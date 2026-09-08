"""Der eine Ort fuer Kategoriefragen der Oberflaeche.

Warum ueberhaupt ein eigener Dienst: Die Bausteine liegen seit jeher im
eBay-Client - ``suggest_categories_mit_namen`` fuer die Auswahlliste,
``get_required_aspects`` fuer die Pflichtmerkmale. Es fehlte nur die Verdrahtung.
Sie in den Router zu schreiben haette zwei Aufrufer denselben Umgang mit Fehlern
und Attrappen nachbauen lassen; hier steht er einmal.

Zwei Dinge, die dieser Dienst BEWUSST nicht tut:

* Er raet nicht. ``build_aspects`` im Client fuellt fehlende Pflichtmerkmale mit
  dem ersten erlaubten Wert oder "Sonstige" auf. Beim Veroeffentlichen ist das
  ein Notnagel - sonst lehnt eBay ab. Im Formular waere es eine Luege: es
  dichtete dem Betreiber eine Marke an, die er nie eingegeben hat. Hier werden
  Pflichtmerkmale nur GEZEIGT.
* Er behauptet keine Gewissheit. Die Taxonomie-Aufrufe des Clients verschlucken
  jede Ausnahme und geben ``[]`` zurueck. Ein abgelaufener Token ist damit von
  "diese Kategorie hat keine Pflichtmerkmale" nicht zu unterscheiden. Statt ein
  erfundenes ``bekannt``-Flag zu liefern, sagt die Antwort, WOHER sie kommt
  (``quelle``), und die Oberflaeche formuliert entsprechend vorsichtig.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.integrations import get_ebay_client

logger = logging.getLogger("app.services.category")

# eBay-Kategorien sind reine Ziffernfolgen. "0" ist KEINE gueltige Kategorie -
# genau dieser Wert ging frueher an eBay und wurde mit Fehler 25002 abgelehnt.
_NUR_ZIFFERN = re.compile(r"^\d{1,12}$")


class KategorieFehler(ValueError):
    """Die Kategorie-Angabe taugt nicht."""


def pruefe_kategorie_id(wert: Any) -> str:
    """Kategorie-Nummer pruefen. Wirft, statt Unsinn durchzulassen."""
    text = str(wert or "").strip()
    if not text:
        raise KategorieFehler("Keine Kategorie angegeben.")
    if not _NUR_ZIFFERN.match(text):
        raise KategorieFehler(
            f"'{text}' ist keine eBay-Kategorie. Erwartet wird eine Nummer, "
            "zum Beispiel 15687."
        )
    if text.lstrip("0") == "":
        raise KategorieFehler(
            "Die Kategorie '0' gibt es nicht. eBay lehnt sie mit Fehler 25002 ab."
        )
    return text


def _quelle() -> str:
    """Kam die Antwort von eBay oder aus der Attrappe?

    Steht in jeder Antwort, damit die Oberflaeche im Probebetrieb nicht so tut,
    als haette sie mit eBay gesprochen.
    """
    from app.config import get_settings

    return "attrappe" if get_settings().use_mock("ebay") else "ebay"


async def vorschlaege(suchtext: str, *, limit: int = 6) -> dict:
    """Kategorie-Vorschlaege in Klartext: ``[{id, name, pfad}]``.

    Leere Liste bleibt leer - lieber keine Auswahl als eine erfundene.
    """
    client = get_ebay_client()
    # getattr, weil die abstrakte Basis EbayClient keine Taxonomie-Methode kennt.
    # Ein Client ohne diese Faehigkeit soll eine leere Liste ergeben, keinen
    # AttributeError, der wie ein Programmfehler aussieht.
    holen = getattr(client, "suggest_categories_mit_namen", None)
    if holen is None:
        logger.info("Client kann keine benannten Kategorie-Vorschlaege")
        return {"vorschlaege": [], "quelle": _quelle()}

    gefunden = await holen((suchtext or "").strip(), limit=limit)
    return {"vorschlaege": gefunden or [], "quelle": _quelle()}


async def merkmale(category_id: str) -> dict:
    """Merkmale einer Kategorie: ``[{name, required, variation, values}]``.

    Geliefert werden ALLE Merkmale, nicht nur die Pflichtfelder - ``required``
    trennt sie. Die Oberflaeche braucht beides: Pflicht zum Ausfuellen, Kuer zum
    Anbieten.
    """
    kid = pruefe_kategorie_id(category_id)
    client = get_ebay_client()
    holen = getattr(client, "get_required_aspects", None)
    if holen is None:
        return {"category_id": kid, "merkmale": [], "quelle": _quelle()}

    alle = await holen(kid) or []
    return {
        "category_id": kid,
        "merkmale": alle,
        "pflicht_anzahl": sum(1 for a in alle if a.get("required")),
        "quelle": _quelle(),
    }


def name_aus_vorschlag(vorschlag: dict) -> str | None:
    """Anzeigenamen im Format bauen, das der Rest des Systems erwartet.

    ``pricing.commission_pct`` liest nur den Text VOR dem ersten Doppelpunkt, um
    die eBay-Provision der Warengruppe zu bestimmen. Ein blosser Blattname wie
    "Herren-T-Shirts" laesst die Provision still auf die Pauschale fallen -
    deshalb steht der oberste Zweig vorne.

    Fehlt der Pfad, kommt None zurueck statt eines halben Namens. Eine Luecke ist
    ehrlicher als ein Name, auf den sich eine Provisionsrechnung stuetzt.
    """
    name = (vorschlag or {}).get("name") or ""
    pfad = (vorschlag or {}).get("pfad") or ""
    if not name:
        return None
    if not pfad:
        return None
    oberster = pfad.split(">")[0].strip()
    if not oberster:
        return None
    return f"{oberster}: {name}"
