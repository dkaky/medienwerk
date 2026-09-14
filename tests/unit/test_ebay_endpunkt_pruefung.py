"""Die Diagnose des eBay-Deletion-Endpunkts - netzwerkfrei.

eBay sagt bei einer gescheiterten Validierung nur "failed". Die Pruefung muss
deshalb selbst herausfinden, woran es liegt - vor allem an fehlenden
Worker-Variablen, die JavaScript still durch den Text "undefined" ersetzt.
"""
from __future__ import annotations

import hashlib

from app.integrations.ebay import RealEbayClient
from scripts import pruefe_ebay_endpunkt as p

URL = "https://ebay-deletion.medienwerk.workers.dev"
TOKEN = "a" * 40


def test_rechnung_ist_dieselbe_wie_in_der_app():
    """Skript und App duerfen nie verschieden rechnen."""
    assert p.erwartet("c0de", TOKEN, URL) == RealEbayClient.account_deletion_challenge_response(
        "c0de", TOKEN, URL)
    assert p.erwartet("c0de", TOKEN, URL) == hashlib.sha256(
        ("c0de" + TOKEN + URL).encode()).hexdigest()


def test_beide_variablen_fehlen_wird_erkannt():
    antwort = p.erwartet("c0de", "undefined", "undefined")
    assert "BEIDE" in p.deute_antwort(antwort, "c0de", URL)


def test_fehlender_token_wird_erkannt():
    antwort = p.erwartet("c0de", "undefined", URL)
    assert "VERIFICATION_TOKEN" in p.deute_antwort(antwort, "c0de", URL)


def test_richtige_antwort_meldet_keinen_fehler():
    assert p.deute_antwort(p.erwartet("c0de", TOKEN, URL), "c0de", URL) is None


def test_url_formfehler():
    assert p.url_hinweise(URL) == []
    assert any("'/'" in h for h in p.url_hinweise(URL + "/"))
    assert any("https" in h for h in p.url_hinweise("http://ebay-deletion.x.workers.dev"))


def test_token_regeln():
    assert p.token_hinweise(TOKEN) == []
    assert any("Zeichen" in h for h in p.token_hinweise("zu-kurz"))
    assert any("Leerzeichen" in h for h in p.token_hinweise(" " + TOKEN))
    assert any("ausser" in h for h in p.token_hinweise("ä" * 40))
