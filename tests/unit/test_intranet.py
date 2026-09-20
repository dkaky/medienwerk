"""Betrieb im Internet: Start-Schutz und ein Server-Paket ohne Geheimnisse."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.main import MIN_PASSWORT_LAENGE, pruefe_betrieb

WURZEL = Path(__file__).resolve().parents[2]
PAKET = WURZEL / "deploy" / "intranet"


def test_produktion_ohne_passwort_startet_nicht():
    with pytest.raises(RuntimeError, match="DASHBOARD_PASSWORT"):
        pruefe_betrieb(Settings(app_env="production", dashboard_password=""))


def test_produktion_mit_kurzem_passwort_startet_nicht():
    with pytest.raises(RuntimeError):
        pruefe_betrieb(Settings(app_env="production", dashboard_password="x" * (MIN_PASSWORT_LAENGE - 1)))


def test_produktion_mit_langem_passwort_startet():
    pruefe_betrieb(Settings(app_env="production", dashboard_password="x" * MIN_PASSWORT_LAENGE))


def test_lokal_ohne_passwort_bleibt_moeglich():
    pruefe_betrieb(Settings(app_env="development", dashboard_password=""))


def test_abbild_enthaelt_keine_geheimnisse_und_keine_historie():
    ignore = (WURZEL / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for muss in (".git", ".env", "data", "backups", "*.txt"):
        assert muss in ignore, f".dockerignore laesst {muss} durch"
    docker = (PAKET / "Dockerfile").read_text(encoding="utf-8")
    assert "--proxy-headers" in docker, "Ohne Proxy-Kopfzeilen setzt das Login-Cookie kein Secure"
    for verboten in (".env", "COPY data", "COPY .git"):
        assert verboten not in docker.replace("--env-file", "")


def test_vorlage_der_zugangsdaten_ist_leer():
    text = (PAKET / "env.beispiel").read_text(encoding="utf-8")
    for zeile in text.splitlines():
        if zeile.startswith(("OPENAI_API_KEY=", "EBAY_CLIENT_SECRET=", "EBAY_REFRESH_TOKEN=")):
            assert zeile.split("=", 1)[1] == "", f"Echter Wert in der Vorlage: {zeile.split('=')[0]}"
    assert "APP_ENV=production" in text


def test_nur_caddy_ist_aus_dem_internet_erreichbar():
    compose = (PAKET / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"8030:8030"' not in compose, "Die Anwendung darf nicht direkt im Internet stehen"
    assert '"443:443"' in compose


# --------------------------------------------------------------------------
# Benutzername + Passwort
# --------------------------------------------------------------------------
@pytest.fixture
def mit_benutzer(monkeypatch):
    from fastapi.testclient import TestClient

    from app import auth
    from app.config import get_settings
    from app.main import app

    s = get_settings()
    monkeypatch.setattr(s, "dashboard_user", "medienwerk")
    monkeypatch.setattr(s, "dashboard_password", "testpasswort1")
    auth._failed.clear()
    with TestClient(app, follow_redirects=False) as c:
        yield c
    auth._failed.clear()


def test_login_verlangt_benutzername_und_passwort(mit_benutzer):
    seite = mit_benutzer.get("/login").text
    assert 'name="benutzer"' in seite and 'name="password"' in seite
    ok = mit_benutzer.post("/login", data={"benutzer": "medienwerk", "password": "testpasswort1"})
    assert ok.status_code == 303 and "podshop_session" in ok.headers["set-cookie"]


def test_falscher_benutzer_oder_falsches_passwort_kommt_nicht_rein(mit_benutzer):
    for daten in ({"benutzer": "anderer", "password": "testpasswort1"},
                  {"benutzer": "medienwerk", "password": "falsch"},
                  {"benutzer": "", "password": "testpasswort1"}):
        r = mit_benutzer.post("/login", data=daten)
        assert r.status_code == 401 and "Benutzername oder Passwort falsch" in r.text


def test_ohne_benutzernamen_in_der_einstellung_genuegt_das_passwort(monkeypatch):
    from app.auth import _render_login
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "dashboard_user", "")
    assert 'name="benutzer"' not in _render_login()
