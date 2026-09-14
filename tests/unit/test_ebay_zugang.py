"""Das Verknuepfen des eBay-Zugangs - netzwerkfrei.

Kern ist eine Zusage: Der Refresh-Token landet in der .env und NICHT auf dem
Bildschirm. Frueher stand er im Terminal mit der Bitte, ihn von Hand zu kopieren -
ein Schluessel mit rund 18 Monaten Laufzeit im Verlauf, und jedes verlorene Zeichen
endete in einem wortkargen ``invalid_grant``.
"""
from __future__ import annotations

import json

import httpx

from app.config import Settings
from scripts import ebay_oauth, verify_ebay


def _settings(**over) -> Settings:
    base = dict(ebay_client_id="cid", ebay_client_secret="sec", ebay_refresh_token="",
                ebay_runame="Medienwerk-RuName", ebay_use_sandbox=False)
    base.update(over)
    return Settings(**base)


def _transport(status: int, body: dict) -> httpx.MockTransport:
    def antwort(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json"})
    return httpx.MockTransport(antwort)


# --------------------------------------------------------------------------
# Token-Tausch
# --------------------------------------------------------------------------
def test_token_landet_in_der_env_und_nicht_auf_dem_bildschirm(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("# Kopf\nOPENAI_API_KEY=bleibt\nEBAY_CLIENT_ID=cid\n", encoding="utf-8")
    code = ebay_oauth.exchange(
        "Medienwerk-RuName", "v%5E1.1-code", s=_settings(), env_datei=env,
        transport=_transport(200, {"refresh_token": "rt-geheim-123",
                                   "refresh_token_expires_in": 47304000}))
    assert code == 0
    inhalt = env.read_text(encoding="utf-8")
    assert "EBAY_REFRESH_TOKEN=rt-geheim-123" in inhalt
    # Nichts anderes darf verloren gehen - die .env traegt weitere Schluessel.
    assert "# Kopf" in inhalt and "OPENAI_API_KEY=bleibt" in inhalt
    assert "rt-geheim-123" not in capsys.readouterr().out


def test_vorhandener_token_wird_ersetzt_nicht_verdoppelt(tmp_path):
    env = tmp_path / ".env"
    env.write_text("EBAY_REFRESH_TOKEN=alt\nX=1\n", encoding="utf-8")
    ebay_oauth.speichere_refresh_token("neu", env)
    inhalt = env.read_text(encoding="utf-8")
    assert inhalt.count("EBAY_REFRESH_TOKEN=") == 1
    assert "EBAY_REFRESH_TOKEN=neu" in inhalt and "X=1" in inhalt


def test_abgelaufener_code_laesst_die_env_unveraendert(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("EBAY_REFRESH_TOKEN=bisheriger\n", encoding="utf-8")
    code = ebay_oauth.exchange(
        "Medienwerk-RuName", "alter-code", s=_settings(), env_datei=env,
        transport=_transport(400, {"error": "invalid_grant"}))
    assert code == 2
    assert env.read_text(encoding="utf-8") == "EBAY_REFRESH_TOKEN=bisheriger\n"
    assert "5 min" in capsys.readouterr().out


def test_runame_kommt_aus_der_env_wenn_kein_argument():
    s = _settings(ebay_runame="Aus-Der-Env")
    assert ebay_oauth.runame_aus(None, s) == "Aus-Der-Env"
    assert ebay_oauth.runame_aus("Vom-Argument", s) == "Vom-Argument"
    assert ebay_oauth.runame_aus(None, _settings(ebay_runame="")) == ""


def test_zustimmungsadresse_traegt_runame_und_verkaeufer_berechtigungen():
    url = ebay_oauth.build_authurl("Medienwerk-RuName", _settings())
    assert url.startswith("https://auth.ebay.com/oauth2/authorize?")
    assert "redirect_uri=Medienwerk-RuName" in url
    # sell.account braucht der Betrieb fuer Richtlinien und Standorte.
    assert "sell.account" in url


def test_fehlende_app_schluessel_werden_beim_namen_genannt():
    s = _settings(ebay_client_id="", ebay_client_secret="")
    assert ebay_oauth.fehlende_zugangsdaten(s) == ["EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET"]
    assert ebay_oauth.fehlende_zugangsdaten(_settings()) == []


# --------------------------------------------------------------------------
# Verbindungstest: Bewertung ohne Netz
# --------------------------------------------------------------------------
def test_alter_standort_wird_gemeldet():
    hinweise = verify_ebay.bewerte_standorte(
        [{"merchantLocationKey": "FM-DE-01"}, {"merchantLocationKey": "MW-DE-01"}], "MW-DE-01")
    assert len(hinweise) == 1 and "FM-DE-01" in hinweise[0]


def test_fehlender_eigener_standort_ist_nur_ein_hinweis():
    hinweise = verify_ebay.bewerte_standorte([], "MW-DE-01")
    assert len(hinweise) == 1 and "MW-DE-01" in hinweise[0]


def test_sauberer_standort_ohne_hinweis():
    assert verify_ebay.bewerte_standorte([{"merchantLocationKey": "MW-DE-01"}], "MW-DE-01") == []


def test_fehlende_richtlinien_werden_benannt():
    hinweise = verify_ebay.bewerte_richtlinien({"payment": "1", "fulfillment": None, "return": ""})
    assert len(hinweise) == 1
    assert "Versand" in hinweise[0] and "Ruecknahme" in hinweise[0] and "Zahlung" not in hinweise[0]
    assert verify_ebay.bewerte_richtlinien({"payment": "1", "fulfillment": "2", "return": "3"}) == []
