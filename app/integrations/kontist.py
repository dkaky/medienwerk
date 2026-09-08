"""Kontist-Geschaeftskonto (NUR-LESE-Anbindung, Nutzer-Projekt 10.08.).

OAuth2 Authorization-Code-Flow mit Refresh-Token (Scope ``offline``); GraphQL-API
fuer Kontostand (inkl. Kontist-Aufteilung yours/tax/vat) und Transaktionen.

SICHERHEIT: Der Scope ``transfers`` wird BEWUSST NIE angefragt — diese Anbindung
kann kein Geld bewegen. client_id/secret liegen nur in der Server-.env; die Tokens
in ``data/kontist_token.json`` (gitignored). Betraege kommen von Kontist in CENT.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

from app.config import get_settings

logger = logging.getLogger("app.integrations.kontist")

AUTH_URL = "https://api.kontist.com/api/oauth/authorize"
TOKEN_URL = "https://api.kontist.com/api/oauth/token"
GRAPHQL_URL = "https://api.kontist.com/api/graphql"
# BEWUSST ohne 'transfers' (kein Geld bewegen) — nur Einsicht.
SCOPES = "accounts transactions statements offline"
TOKEN_FILE = Path("data/kontist_token.json")

_SUMMARY_QUERY = """
{
  viewer {
    mainAccount {
      balance
      availableBalance
      stats { accountBalance yours vatTotal taxTotal }
      transactions(first: 20) {
        edges { node { amount name valutaDate category } }
      }
    }
  }
}
"""


# Bank-Sync (bank_sync_service): alle Buchungen paginiert, neueste zuerst.
_TX_PAGE_QUERY = """
query BankSync($first: Int!, $after: String) {
  viewer {
    mainAccount {
      transactions(first: $first, after: $after) {
        edges { node { id amount name iban type bookingDate valutaDate purpose
                       paymentMethod foreignCurrency originalAmount } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


# EIGENE Abfrage fuer die Beleg-Anhaenge. Bewusst getrennt von _TX_PAGE_QUERY:
# die steckt im naechtlichen Bank-Sync, und ein GraphQL-Fehler dort wuerde die
# komplette Kontist-Spiegelung abbrechen.
_TX_ASSETS_QUERY = """
query BelegAnhaenge($first: Int!, $after: String) {
  viewer {
    mainAccount {
      transactions(first: $first, after: $after) {
        edges { node { id bookingDate hasAssets receiptName
                       documentNumber documentType documentDownloadUrl
                       assets { id name filetype fullsize }
                       transactionAssets { id name filetype fullsize } } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


async def fetch_transaction_assets(max_pages: int = 40) -> list[dict]:
    """Buchungen mit ihren in Kontist hinterlegten Belegen.

    Nur die Anhang-Felder — der eigentliche Buchungsabruf bleibt unberuehrt.
    """
    out: list[dict] = []
    after: str | None = None
    for _ in range(max_pages):
        data = await _graphql(_TX_ASSETS_QUERY, {"first": 50, "after": after})
        conn = ((((data.get("data") or {}).get("viewer") or {})
                 .get("mainAccount") or {}).get("transactions") or {})
        out.extend(e["node"] for e in (conn.get("edges") or []) if e.get("node"))
        seite = conn.get("pageInfo") or {}
        if not seite.get("hasNextPage"):
            break
        after = seite.get("endCursor")
    return out


async def lade_anhang(url: str) -> bytes:
    """Beleg-Datei herunterladen. Mit Zugangstoken, falls Kontist ihn verlangt."""
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await c.get(url)
        if r.status_code in (401, 403):
            tok = (_load_tokens() or {}).get("access_token")
            r = await c.get(url, headers={"Authorization": f"Bearer {tok}"})
        r.raise_for_status()
        return r.content


_FELDER_QUERY = """
query Felder($typ: String!) {
  __type(name: $typ) { name fields { name type { name kind ofType { name kind } } } }
}
"""


async def felder_von(typ: str = "Transaction") -> dict:
    """DIAGNOSE: welche Felder hat dieser GraphQL-Typ wirklich?

    Raten waere gefaehrlich: ein unbekanntes Feld laesst die GraphQL-Abfrage fehlschlagen,
    und die Buchungs-Abfrage steckt im naechtlichen Bank-Sync. Also erst nachsehen.
    """
    data = await _graphql(_FELDER_QUERY, {"typ": typ})
    t = (data.get("data") or {}).get("__type") or {}
    return {"typ": t.get("name"),
            "felder": [f["name"] for f in (t.get("fields") or [])]}


async def fetch_transactions(max_pages: int = 40) -> list[dict]:
    """Alle Kontobuchungen paginiert abrufen (50/Seite, Cap 40 Seiten = 2000 Stk.).

    Der Cap liegt weit ueber dem Bestand des jungen Kontos und unter dem
    Kontist-Rate-Limit (<100 Requests/min). Wirft PermissionError wenn nicht
    verbunden (Aufrufer behandeln das als "uebersprungen").
    """
    out: list[dict] = []
    after: str | None = None
    for _ in range(max_pages):
        data = await _graphql(_TX_PAGE_QUERY, {"first": 50, "after": after})
        conn = ((((data.get("data") or {}).get("viewer") or {})
                 .get("mainAccount") or {}).get("transactions") or {})
        for edge in (conn.get("edges") or []):
            node = (edge or {}).get("node") or {}
            if node.get("id"):
                out.append(node)
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage") or not page.get("endCursor"):
            break
        after = page["endCursor"]
    return out


# GoBD-Archiv (Baustein 3): Original-CSV der Bank je Zeitraum.
_TX_CSV_QUERY = """
query BankArchiv($from: DateTime, $to: DateTime) {
  viewer { mainAccount { transactionsCSV(from: $from, to: $to) } }
}
"""


async def fetch_transactions_csv(date_from: str, date_to: str) -> str:
    """Kontoumsaetze eines Zeitraums als CSV, wie die BANK sie liefert.

    Fuer das GoBD-Archiv ist das Original-Format beweiskraeftiger als eine
    eigene Re-Serialisierung des Spiegels.
    """
    data = await _graphql(_TX_CSV_QUERY, {"from": date_from, "to": date_to})
    acc = (((data.get("data") or {}).get("viewer") or {}).get("mainAccount")) or {}
    return str(acc.get("transactionsCSV") or "")


def is_configured() -> bool:
    s = get_settings()
    return bool(s.kontist_client_id and s.kontist_client_secret)


def is_connected() -> bool:
    """True, wenn ein gespeicherter Access-Token existiert (OAuth-Flow durchlaufen)."""
    return bool((_load_tokens() or {}).get("access_token"))


def redirect_uri() -> str:
    """Wohin Kontist nach dem Zustimmen zurueckschickt.

    Hier stand eine fest eingetragene Adresse - der Server der GbR, von der dieses
    System kopiert wurde. Das war kein Schoenheitsfehler: Bei diesem Rueckruf haengt
    der Autorisierungscode in der Adresszeile. Er waere also an fremde
    Infrastruktur gegangen, und wer dort mitliest, kommt damit an das Bankkonto.

    Fehlt die Einstellung, bricht der Vorgang deshalb ab, statt auf irgendetwas
    zurueckzufallen. Lieber keine Kontoanbindung als eine, die woanders endet.
    """
    ziel = (get_settings().kontist_redirect_uri or "").strip()
    if not ziel:
        raise RuntimeError(
            "KONTIST_REDIRECT_URI ist nicht gesetzt. Die Kontist-Anmeldung braucht "
            "die eigene oeffentliche Rueckruf-Adresse (.../api/v1/kontist/callback). "
            "Ohne sie wird nichts angemeldet - frueher zeigte diese Stelle auf den "
            "Server eines fremden Betriebs."
        )
    return ziel


def authorize_url(state: str) -> str:
    s = get_settings()
    return AUTH_URL + "?" + urlencode({
        "scope": SCOPES, "response_type": "code",
        "client_id": s.kontist_client_id,
        "redirect_uri": redirect_uri(), "state": state,
    })


def _load_tokens() -> dict | None:
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_tokens(tok: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({
        "access_token": tok.get("access_token"),
        "refresh_token": tok.get("refresh_token"),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }, indent=1), encoding="utf-8")


async def _token_request(data: dict) -> dict:
    s = get_settings()
    payload = {"client_id": s.kontist_client_id,
               "client_secret": s.kontist_client_secret, **data}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(TOKEN_URL, data=payload)
        r.raise_for_status()
        return r.json()


async def exchange_code(code: str) -> None:
    """Authorization-Code gegen Access+Refresh-Token tauschen und speichern."""
    tok = await _token_request({"grant_type": "authorization_code",
                                "code": code, "redirect_uri": redirect_uri()})
    _save_tokens(tok)
    logger.info("kontist verbunden (refresh_token=%s)",
                "ja" if tok.get("refresh_token") else "NEIN")


# Refresh serialisieren: der Refresh-Token ist ein Einmal-Token (Rotation). Zwei
# parallele 401-Pfade (naechtlicher Bank-Sync + offener Dashboard-Tab) wuerden
# sonst denselben Token doppelt einloesen und die Verbindung zerstoeren.
_REFRESH_LOCK = asyncio.Lock()


async def _refresh(stale_access: str | None = None) -> dict | None:
    async with _REFRESH_LOCK:
        cur = _load_tokens()
        if not cur or not cur.get("refresh_token"):
            return None
        if stale_access and cur.get("access_token") and cur["access_token"] != stale_access:
            return cur  # ein paralleler Aufrufer hat waehrend des Wartens erneuert
        try:
            tok = await _token_request({"grant_type": "refresh_token",
                                        "refresh_token": cur["refresh_token"]})
        except Exception as exc:  # noqa: BLE001 – Verbindung gilt dann als getrennt
            logger.warning("kontist refresh fehlgeschlagen: %s", str(exc)[:150])
            return None
        if not tok.get("refresh_token"):
            tok["refresh_token"] = cur["refresh_token"]
        _save_tokens(tok)
        return _load_tokens()


async def _graphql(query: str, variables: dict | None = None) -> dict:
    tokens = _load_tokens()
    if not tokens or not tokens.get("access_token"):
        raise PermissionError("nicht verbunden")
    payload: dict = {"query": query}
    if variables:
        payload["variables"] = variables
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(GRAPHQL_URL, json=payload, headers={
            "Authorization": f"Bearer {tokens['access_token']}"})
        data = _safe_json(r)
        # Abgelaufener Access-Token kommt bei Kontist als HTTP 401 ODER als
        # GraphQL-Fehler "Unauthorized" mit HTTP 200 (live beobachtet 11.08.,
        # Token-Laufzeit ~1 h) -> in beiden Faellen einmal refreshen + retry.
        if r.status_code == 401 or _graphql_unauthorized(data):
            tokens = await _refresh(tokens.get("access_token"))
            if not tokens:
                raise PermissionError("Token abgelaufen — bitte neu verbinden")
            r = await client.post(GRAPHQL_URL, json=payload, headers={
                "Authorization": f"Bearer {tokens['access_token']}"})
            data = _safe_json(r)
        r.raise_for_status()
        if data is None:
            raise RuntimeError("Kontist: keine JSON-Antwort")
        # GraphQL meldet Fehler mit HTTP 200 + errors-Array. NICHT verschlucken —
        # sonst laeuft z.B. der Bank-Sync scheinbar erfolgreich mit 0 Buchungen.
        if data.get("errors"):
            if _graphql_unauthorized(data):
                raise PermissionError("Token abgelaufen — bitte neu verbinden")
            msg = "; ".join(str((e or {}).get("message", "?"))
                            for e in data["errors"][:3])
            raise RuntimeError(f"Kontist GraphQL: {msg[:300]}")
        return data


def _safe_json(r: httpx.Response) -> dict | None:
    try:
        data = r.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _graphql_unauthorized(data: dict | None) -> bool:
    """True, wenn das errors-Array eine Auth-Ablehnung traegt (Unauthorized/
    Unauthenticated) — Kontist liefert die teils mit HTTP 200 aus."""
    for e in (data or {}).get("errors") or []:
        msg = str((e or {}).get("message", "")).lower()
        if "unauthor" in msg or "unauthentic" in msg:
            return True
    return False


def _cent(v) -> float | None:
    """Kontist liefert Betraege in CENT (Ganzzahl) -> EUR."""
    try:
        return round(int(v) / 100.0, 2) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_summary(data: dict) -> dict:
    """GraphQL-Antwort -> kompakte EUR-Zusammenfassung (rein, testbar)."""
    acc = (((data.get("data") or {}).get("viewer") or {}).get("mainAccount")) or {}
    stats = acc.get("stats") or {}
    tx = []
    for e in ((acc.get("transactions") or {}).get("edges") or []):
        n = e.get("node") or {}
        tx.append({"amount_eur": _cent(n.get("amount")), "name": n.get("name"),
                   "date": n.get("valutaDate"), "category": n.get("category")})
    return {
        "balance_eur": _cent(acc.get("balance")),
        "available_eur": _cent(acc.get("availableBalance")),
        "yours_eur": _cent(stats.get("yours")),
        "tax_eur": _cent(stats.get("taxTotal")),
        "vat_eur": _cent(stats.get("vatTotal")),
        "transactions": tx,
    }


async def summary() -> dict:
    """Kontostand + letzte Transaktionen. Nie werfen — Status-Feld statt Fehler."""
    if not is_configured():
        return {"configured": False, "connected": False}
    if not is_connected():
        return {"configured": True, "connected": False}
    try:
        data = await _graphql(_SUMMARY_QUERY)
    except PermissionError:
        return {"configured": True, "connected": False}
    except Exception as exc:  # noqa: BLE001 – Anzeige-Feature, nie das Dashboard sprengen
        logger.warning("kontist summary fehlgeschlagen: %s", str(exc)[:150])
        return {"configured": True, "connected": True, "error": str(exc)[:120]}
    return {"configured": True, "connected": True, **parse_summary(data)}
