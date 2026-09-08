"""Tests fuer den Dashboard-Login (app/auth.py).

Auth ist standardmaessig AUS (kein DASHBOARD_PASSWORD in der Test-Env); die
Fixture `auth_client` schaltet sie gezielt ein und raeumt danach wieder auf.
"""
from __future__ import annotations

import time

import pytest

from app import auth as auth_mod
from app.config import get_settings

PASSWORD = "test-geheim-123"


@pytest.fixture
def auth_client():
    """TestClient mit aktiviertem Passwortschutz (Settings sind lru-gecacht)."""
    settings = get_settings()
    settings.dashboard_password = PASSWORD
    auth_mod._failed.clear()
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client
    settings.dashboard_password = ""
    auth_mod._failed.clear()


def _login(client, password=PASSWORD):
    return client.post("/login", data={"password": password}, follow_redirects=False)


# --- Auth aus (Default) ---------------------------------------------------


def test_auth_disabled_by_default(client):
    assert client.get("/", follow_redirects=False).status_code == 200
    # Login-Seite verweist bei ausgeschalteter Auth zurueck aufs Dashboard.
    assert client.get("/login", follow_redirects=False).status_code == 303


# --- Zugriff ohne Session --------------------------------------------------


def test_browser_redirected_to_login(auth_client):
    r = auth_client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_api_gets_401_json(auth_client):
    r = auth_client.get("/api", follow_redirects=False)
    assert r.status_code == 401
    assert r.json()["detail"] == "Nicht angemeldet"


def test_docs_protected(auth_client):
    assert auth_client.get("/docs", follow_redirects=False).status_code == 401
    assert auth_client.get("/openapi.json", follow_redirects=False).status_code == 401


def test_public_paths_stay_open(auth_client):
    assert auth_client.get("/health").status_code == 200
    # Build-Info (welcher Commit laeuft) muss auch ohne Login abrufbar sein.
    rv = auth_client.get("/api/v1/version")
    assert rv.status_code == 200
    body = rv.json()
    assert "commit" in body and "started_at" in body
    # eBay-Deletion-Challenge MUSS ohne Login funktionieren (eBay ruft sie auf).
    r = auth_client.get(
        "/ebay/marketplace-account-deletion", params={"challenge_code": "abc"}
    )
    assert r.status_code == 200
    assert "challengeResponse" in r.json()
    # POST-Notification ebenfalls oeffentlich (Ack immer 200).
    r = auth_client.post("/ebay/marketplace-account-deletion", json={})
    assert r.status_code == 200


# --- Login-Flow -------------------------------------------------------------


def test_login_success_sets_cookie_and_grants_access(auth_client):
    r = _login(auth_client)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert auth_mod.COOKIE_NAME in r.cookies
    # Cookie wird vom TestClient uebernommen -> Dashboard + API erreichbar.
    assert auth_client.get("/", follow_redirects=False).status_code == 200
    assert auth_client.get("/health").status_code == 200


def test_login_wrong_password(auth_client):
    r = _login(auth_client, password="falsch")
    assert r.status_code == 401
    assert auth_mod.COOKIE_NAME not in r.cookies


def test_rate_limit_locks_after_max_attempts(auth_client):
    for _ in range(auth_mod._MAX_ATTEMPTS):
        assert _login(auth_client, password="falsch").status_code == 401
    # Gesperrt – selbst das RICHTIGE Passwort wird jetzt abgewiesen.
    assert _login(auth_client).status_code == 429


def test_rate_limit_reset_after_success(auth_client):
    for _ in range(auth_mod._MAX_ATTEMPTS - 1):
        _login(auth_client, password="falsch")
    assert _login(auth_client).status_code == 303
    assert auth_mod._failed == {}


# --- Token-Sicherheit -------------------------------------------------------


def test_forged_cookie_rejected(auth_client):
    for bad in ["", "kaputt", "123.abc", "999999999999.deadbeef"]:
        auth_client.cookies.set(auth_mod.COOKIE_NAME, bad)
        assert auth_client.get("/", follow_redirects=False).status_code == 401


def test_non_ascii_cookie_gives_401_not_500(auth_client):
    # Cookie-Header werden latin-1-dekodiert: Non-ASCII in der Signatur darf
    # keinen TypeError/500 ausloesen, sondern muss sauber als 401 abgewiesen werden.
    assert auth_mod.verify_token("123456.caf\xe9") is False
    assert auth_mod.verify_token("\xa0123.abc") is False
    assert auth_mod.verify_token("١٢٣.abc") is False
    auth_client.cookies.set(auth_mod.COOKIE_NAME, "123456.cafe-xyz")
    assert auth_client.get("/", follow_redirects=False).status_code == 401


def test_expired_token_rejected(auth_client):
    expired = auth_mod.issue_token(now=time.time() - auth_mod.SESSION_TTL_SECONDS - 60)
    assert not auth_mod.verify_token(expired)
    auth_client.cookies.set(auth_mod.COOKIE_NAME, expired)
    assert auth_client.get("/", follow_redirects=False).status_code == 401


def test_token_invalidated_by_password_change(auth_client):
    token = auth_mod.issue_token()
    assert auth_mod.verify_token(token)
    get_settings().dashboard_password = "neues-passwort"
    assert not auth_mod.verify_token(token)


def test_logout_clears_session(auth_client):
    _login(auth_client)
    assert auth_client.get("/", follow_redirects=False).status_code == 200
    r = auth_client.get("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert auth_client.get("/", follow_redirects=False).status_code == 401
