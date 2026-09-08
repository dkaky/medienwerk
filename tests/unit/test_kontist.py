"""Kontist-Anbindung (Nur-Lese): URL-Bau, Cent-Umrechnung, Summary-Parsing."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.integrations import kontist


_EIGENER_RUECKRUF = "https://beispiel.invalid/api/v1/kontist/callback"


def test_authorize_url_never_requests_transfers(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "kontist_client_id", "cid-123")
    monkeypatch.setattr(get_settings(), "kontist_redirect_uri", _EIGENER_RUECKRUF)
    u = kontist.authorize_url("state-x")
    assert u.startswith(kontist.AUTH_URL)
    assert "transfers" not in u, "die Anbindung darf NIE Geld bewegen koennen"
    assert "offline" in u and "cid-123" in u and "state-x" in u


def test_rueckruf_ohne_einstellung_bricht_ab(monkeypatch):
    """Ohne eigene Rueckruf-Adresse wird gar nichts angemeldet.

    An dieser Stelle stand fest eingetragen der Server der GbR, von der dieses
    System kopiert wurde. Beim Rueckruf haengt der Autorisierungscode in der
    Adresszeile - er waere also an fremde Infrastruktur gegangen. Ein leerer
    Wert muss deshalb hoerbar abbrechen und darf auf nichts zurueckfallen.
    """
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "kontist_redirect_uri", "")
    with pytest.raises(RuntimeError, match="KONTIST_REDIRECT_URI"):
        kontist.redirect_uri()


def test_rueckruf_zeigt_nie_auf_fremde_infrastruktur():
    """Der Quelltext darf die alte Adresse nirgends mehr enthalten."""
    quelle = Path(kontist.__file__).read_text(encoding="utf-8")
    assert "sslip.io" not in quelle
    assert "167-233-36-190" not in quelle


def test_parse_summary_converts_cents():
    data = {"data": {"viewer": {"mainAccount": {
        "balance": 123456, "availableBalance": 100000,
        "stats": {"yours": 80000, "taxTotal": 30000, "vatTotal": 13456},
        "transactions": {"edges": [
            {"node": {"amount": -1999, "name": "AliExpress", "valutaDate": "2026-08-10",
                      "category": None}}]}}}}}
    out = kontist.parse_summary(data)
    assert out["balance_eur"] == 1234.56 and out["available_eur"] == 1000.0
    assert out["yours_eur"] == 800.0 and out["tax_eur"] == 300.0
    assert out["transactions"][0]["amount_eur"] == -19.99


def test_graphql_unauthorized_detection():
    # Kontist meldet abgelaufene Tokens teils als GraphQL-Fehler mit HTTP 200 —
    # der Refresh-Pfad muss auch DIESE Form erkennen (Live-Vorfall 11.08.).
    assert kontist._graphql_unauthorized({"errors": [{"message": "Unauthorized"}]})
    assert kontist._graphql_unauthorized({"errors": [{"message": "User unauthenticated"}]})
    assert not kontist._graphql_unauthorized({"errors": [{"message": "Rate limited"}]})
    assert not kontist._graphql_unauthorized({"data": {"viewer": {}}})
    assert not kontist._graphql_unauthorized(None)


def test_token_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(kontist, "TOKEN_FILE", tmp_path / "tok.json")
    assert kontist._load_tokens() is None
    kontist._save_tokens({"access_token": "a", "refresh_token": "r"})
    tok = kontist._load_tokens()
    assert tok["access_token"] == "a" and tok["refresh_token"] == "r"
