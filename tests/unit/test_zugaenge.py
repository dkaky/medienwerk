"""Der Zugaenge-Ueberblick: was ist eingerichtet, was fehlt, was ist Attrappe.

Vorher gab es diesen Ort nicht. Der DHL-Schluessel war der EINZIGE Zugang, der
ueberhaupt im Bild vorkam - und er hing mitten im Tagesgeschaeft der
Bestellungen. eBay und die KI tragen den Betrieb und waren
unsichtbar.

Zwei Eigenschaften entscheiden, ob dieser Ueberblick etwas taugt:

* **Er verraet nichts.** Ein Zugangsschluessel darf nie in einer Antwort
  auftauchen - auch nicht gekuerzt, auch nicht versehentlich ueber ein Feld, das
  jemand spaeter hinzufuegt. Deshalb prueft ein Test die GANZE Antwort gegen die
  echten Werte aus der Konfiguration.
* **Er behauptet nichts.** "Eingerichtet" heisst, dass ein Feld gefuellt ist -
  nicht, dass der Zugang funktioniert. Ein abgelaufener Token sieht von hier aus
  wie ein gueltiger. Wer daraus eine gruene Ampel machte, baute eine Luege, die
  erst auffliegt, wenn ein Angebot nicht rausgeht.
"""

from __future__ import annotations

import json

from app.config import get_settings
from app.services.zugaenge_service import uebersicht


def test_kein_einziger_wert_gelangt_nach_aussen():
    """Die wichtigste Eigenschaft: die Antwort enthaelt keine Zugangsdaten.

    Geprueft wird die vollstaendige Antwort als Text gegen jeden nicht-trivialen
    Wert aus der Konfiguration. Ein spaeter hinzugefuegtes Feld, das versehentlich
    einen Schluessel durchreicht, faellt hier auf.
    """
    s = get_settings()
    text = json.dumps(uebersicht(), ensure_ascii=False)

    geheim = [
        "ebay_client_id", "ebay_client_secret", "ebay_refresh_token",
        "ebay_ipn_verification_token", "aliexpress_app_key",
        "aliexpress_app_secret", "aliexpress_access_token",
        "aliexpress_refresh_token", "llm_api_key", "printify_token",
        "fal_api_key", "kontist_client_secret", "dhl_api_key",
    ]
    for feld in geheim:
        wert = str(getattr(s, feld, "") or "").strip()
        # Kurze Werte waeren Zufallstreffer ("de", "0"). Nur echte Schluessel pruefen.
        if len(wert) >= 8:
            assert wert not in text, f"{feld} steht in der Antwort"


def test_jeder_zugang_hat_die_noetigen_angaben():
    d = uebersicht()
    assert d["zugaenge"], "Es muss mindestens ein Zugang gelistet sein"
    for z in d["zugaenge"]:
        for feld in ("schluessel", "name", "wofuer", "eingerichtet",
                     "fehlend", "attrappe", "hinweise", "noetig"):
            assert feld in z, f"{z.get('name')} fehlt das Feld {feld}"
        assert isinstance(z["eingerichtet"], bool)
        assert isinstance(z["fehlend"], list)


def test_die_tragenden_zugaenge_sind_dabei():
    """eBay und die KI tragen den Betrieb - sie duerfen nie fehlen."""
    d = uebersicht()
    vorhanden = {z["schluessel"] for z in d["zugaenge"]}
    assert {"ebay", "llm"} <= vorhanden
    assert "aliexpress" not in vorhanden


def test_freiwilliges_ist_als_freiwillig_erkennbar():
    """Ein fehlender DHL-Schluessel ist kein Mangel - das muss unterscheidbar sein."""
    d = uebersicht()
    nach_schluessel = {z["schluessel"]: z for z in d["zugaenge"]}
    assert nach_schluessel["ebay"]["noetig"] is True
    assert nach_schluessel["dhl"]["noetig"] is False
    assert nach_schluessel["kontist"]["noetig"] is False


def test_attrappen_werden_benannt(monkeypatch):
    """Im Probebetrieb muss dranstehen, dass es eine Attrappe ist.

    Die Tests laufen mit USE_MOCKS=true - genau die Lage, in der der Betrieb
    gerade arbeitet.
    """
    d = uebersicht()
    ebay = next(z for z in d["zugaenge"] if z["schluessel"] == "ebay")
    assert ebay["attrappe"] is True
    assert d["probebetrieb"] is True
    assert "ebay" in d["attrappen"]
    assert any("Probebetrieb" in h for h in ebay["hinweise"])


def test_fehlende_felder_werden_im_klartext_genannt(monkeypatch):
    """'fehlt: Refresh-Token' hilft weiter, 'nicht eingerichtet' nicht."""
    s = get_settings()
    monkeypatch.setattr(s, "ebay_refresh_token", "")
    monkeypatch.setattr(s, "ebay_merchant_location_key", "")

    ebay = next(z for z in uebersicht()["zugaenge"] if z["schluessel"] == "ebay")
    assert ebay["eingerichtet"] is False
    assert "Refresh-Token" in ebay["fehlend"]
    assert "Lagerort" in ebay["fehlend"]


def test_der_hinweis_zur_ungewissheit_steht_in_der_antwort():
    """Die Trennung eingerichtet/funktioniert muss beim Leser ankommen."""
    d = uebersicht()
    assert "nicht, ob er funktioniert" in d["hinweis"]


def test_endpunkt_antwortet(client):
    r = client.get("/api/v1/settings/zugaenge")
    assert r.status_code == 200
    d = r.json()
    assert d["zugaenge"] and "hinweis" in d


def test_oberflaeche_verspricht_keine_verbindung():
    """Kein "Verbindung steht" in der Anzeige - das koennte sie nicht wissen."""
    from pathlib import Path
    seite = Path(__file__).resolve().parents[2] / "app" / "static" / "index.html"
    q = seite.read_text(encoding="utf-8")
    rumpf = q[q.index("async function ladeZugaenge"):]
    rumpf = rumpf[:rumpf.index("async function loadOverview")]
    for verboten in ("Verbindung steht", "verbunden ✓", "funktioniert"):
        assert verboten not in rumpf, f"Die Anzeige behauptet zu viel: {verboten}"
