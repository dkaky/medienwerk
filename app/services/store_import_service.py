"""Shop-Import: die meistverkauften Produkte eines AliExpress-Shops als ENTWUERFE anlegen.

Nutzerwunsch 02.08.: "manchmal gibt es gute Stores, von denen wir gerne weitere Produkte
importieren wollen" – Shop-Link einfuegen, Anzahl waehlen (Standard 25), fertig.

Ablauf: Shop-Seite per Headless-Browser nach Bestsellern auslesen (die DS-API kann NICHT nach
Shop filtern) -> je Produkt der GANZ NORMALE Upload-Weg (``product_service.upload_product``),
also inkl. Scrape, KI-Titel/Beschreibung, Preis-Kalkulation (20 % / min. 4 EUR) und den
bestehenden Schutzfiltern. Es entstehen ENTWUERFE – nichts geht automatisch auf eBay.

Der Lauf ist gedrosselt (AliExpress-Kontingent) und bricht bei Einzelfehlern nicht ab.
"""
from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.integrations import aliexpress_store as store
from app.models import Product
from app.retry import PersistentError

logger = logging.getLogger("app.services.store_import")

MAX_PRODUCTS = 100          # Sicherheitsdeckel je Lauf
_ABBRUCH_NACH = 3           # so viele Fehler in Folge ohne einen Erfolg -> Schluss
MAX_POOL = 150              # so viele Shop-Artikel werden hoechstens durchgesehen

#: Obergrenze fuer die FORTSETZUNG. Ein zweiter Lauf muss die schon gesehenen
#: Artikel erst ueberspringen, bevor er neue findet - fuer die Plaetze 201-300
#: sind also mindestens 300 Artikel durchzusehen. MAX_POOL allein (150) reicht
#: dafuer nie. Diese Grenze verhindert trotzdem, dass ein Lauf endlos scrollt.
MAX_POOL_FORTSETZUNG = 1000

#: Schluessel-Vorsilbe in ``app_settings`` fuer den Fortsetzungspunkt je Shop.
_GESEHEN_SCHLUESSEL = "store_gesehen:"
_PAUSE_S = 1.5              # Drossel zwischen zwei Uploads (API-Kontingent schonen)

# Fortschritt des laufenden Imports (In-Memory, fuer die Anzeige im Dashboard).
_state: dict = {"running": False, "store_id": None, "total": 0, "done": 0,
                "created": 0, "skipped": 0, "failed": 0, "error": None, "letzte": [],
                "gefunden": 0, "abbruch": False, "abgebrochen": False}


_task = None                # starke Referenz, sonst kann der Hintergrund-Lauf wegoptimiert werden


def _state_datei():
    """Ablageort des Fortschritts (neben der Datenbank)."""
    from pathlib import Path

    from app.config import get_settings
    url = get_settings().database_url
    ordner = (Path(url.split("///", 1)[-1]).parent
              if url.startswith("sqlite") else Path("data"))
    ordner.mkdir(parents=True, exist_ok=True)
    return ordner / "store_import_state.json"


def _speichern() -> None:
    """Fortschritt auf Platte schreiben – ueberlebt einen Neustart des Dienstes."""
    import json
    try:
        _state_datei().write_text(json.dumps(_state, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 – Anzeige-Komfort, nie den Lauf stoppen
        logger.debug("store import state nicht speicherbar", extra={"error": str(exc)[:120]})


def startup_check() -> None:
    """Beim Start pruefen, ob ein Import mitten im Lauf abgebrochen wurde.

    Real passiert am 03.08.2026: waehrend eines laufenden Imports wurde der Dienst fuer ein
    Deployment neu gestartet. Der Hintergrund-Lauf starb, im Dashboard blieb die Anzeige
    einfach stehen – der Nutzer sah 11 statt 20 Produkten und keinerlei Hinweis warum.

    Seit 15.08. (Nutzerfrage "wieso muss ich manuell neu druecken?"): liegen
    Wiederanlauf-Infos vor, wird der Lauf AUTOMATISCH fortgesetzt — gefahrlos,
    weil bereits angelegte Artikel uebersprungen werden (idempotent).
    """
    import json
    try:
        alt = json.loads(_state_datei().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(alt, dict) or not alt.get("running"):
        return
    url = str(alt.get("url") or "")
    pids = alt.get("product_ids") or None
    try:
        limit = int(alt.get("limit") or 25)
    except (TypeError, ValueError):
        limit = 25
    if url or pids:
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            global _task
            _state.update(alt)
            _state.update({"running": True, "error": None})
            _speichern()
            _task = loop.create_task(import_store_products_bg(
                store_url_or_id=url, limit=limit, product_ids=pids))
            logger.warning("store import nach Neustart AUTOMATISCH fortgesetzt",
                           extra={"angelegt": alt.get("created"),
                                  "geplant": alt.get("total")})
            return
    alt.update({
        "running": False,
        "error": (f"Import wurde durch einen Neustart des Servers unterbrochen – "
                  f"{alt.get('created') or 0} von {alt.get('total') or '?'} Produkten sind "
                  f"angelegt. Einfach erneut starten: bereits vorhandene Artikel werden "
                  f"übersprungen, es kommen nur die fehlenden dazu."),
    })
    _state.update(alt)
    _speichern()
    logger.warning("store import beim Neustart unterbrochen",
                   extra={"angelegt": alt.get("created"), "geplant": alt.get("total")})


def status() -> dict:
    """Aktueller Stand des Shop-Imports (fuer die Fortschrittsanzeige)."""
    return dict(_state)


def try_reserve() -> bool:
    """Platz fuer EINEN Lauf belegen – SOFORT und synchron.

    Ohne das wuerden zwei schnelle Klicks zwei Browser starten und dieselben Produkte doppelt
    importieren: der Hintergrund-Lauf setzt ``running`` erst, wenn er drankommt – bis dahin
    saehe die zweite Anfrage noch "laeuft nicht".
    """
    if _state.get("running"):
        return False
    _state.update({"running": True, "store_id": None, "total": 0, "done": 0, "created": 0,
                   "skipped": 0, "failed": 0, "error": None, "letzte": [], "gefunden": 0,
                   "url": None, "limit": None, "product_ids": None,
                   "abbruch": False, "abgebrochen": False})
    _speichern()
    return True


def abbrechen() -> dict:
    """Laufenden Import anhalten – nach dem gerade laufenden Produkt.

    Kein hartes Abwuergen: der Lauf haelt zwischen zwei Produkten. Ein mitten im
    Anlegen abgeschnittener Entwurf waere halb im System und muesste von Hand
    aufgeraeumt werden. Bereits erzeugte Entwuerfe BLEIBEN — genau darum geht es
    ("wenn man genug Entwuerfe hat", Nutzerwunsch 19.08.).
    """
    if not _state.get("running"):
        return {"laeuft": False, "hinweis": "Es laeuft gerade kein Import."}
    _state["abbruch"] = True
    _speichern()
    return {"laeuft": True, "abbruch_angefordert": True,
            "bisher_angelegt": _state.get("created", 0)}


def release() -> None:
    """Belegung wieder freigeben (wenn der Lauf gar nicht erst startet)."""
    _state["running"] = False
    _speichern()


def existing_product(db: Session, product_id: str) -> Product | None:
    """Haben wir diese Ware schon? Prueft BEIDE AliExpress-Nummern (3256... und 1005...).

    Ohne die Umrechnung rutschen Duplikate durch: die Shop-Seite liefert die 3256-Form,
    gespeichert ist bei uns oft die 1005-Form derselben Ware.
    """
    if db is None:
        return None
    varianten = store.id_variants(product_id)
    if not varianten:
        return None
    bedingungen = [Product.aliexpress_id.in_(varianten)]
    bedingungen += [Product.aliexpress_url.like(f"%{v}%") for v in varianten]
    return db.scalars(select(Product).where(or_(*bedingungen))).first()


def extract_product_ids(text: str) -> list[str]:
    """Produkt-IDs aus eingefuegtem Text ziehen (Ausweich-Weg fuer Shops, deren
    Seite AliExpress an Server-IPs leer ausliefert — Vorfall Store 1105638009).

    Erkennt /item/<id>-Links und nackte 13-17-stellige IDs. Eine SHOP-URL liefert
    bewusst NICHTS (Shop-Nummern sind kuerzer und stehen hinter /store/)."""
    import re
    raw = str(text or "")
    ids = re.findall(r"/item/(\d{6,20})", raw)
    if not ids and "/store/" not in raw:
        ids = re.findall(r"\b(\d{13,17})\b", raw)
    seen: set[str] = set()
    return [x for x in ids if not (x in seen or seen.add(x))]


def gesehene_ids(db: Session, store_id: str) -> list[str]:
    """Produkt-IDs dieses Shops, die frühere Laeufe schon durchgesehen haben.

    Der Fortsetzungspunkt liegt in ``app_settings`` und nicht im Zustand des
    laufenden Imports: er muss Neustarts ueberleben, sonst faengt jeder Lauf
    wieder bei Platz 1 an.
    """
    from app.services.app_settings import get_app_setting

    # Ohne Sitzung gibt es keinen Fortschritt - das ist kein Fehler. Der Import
    # laesst sich mit ``db=None`` aufrufen, wenn alles Weitere ersetzt ist; dann
    # faengt er eben bei Platz 1 an.
    if db is None or not store_id:
        return []
    roh = get_app_setting(db, f"{_GESEHEN_SCHLUESSEL}{store_id}")
    if not roh:
        return []
    try:
        daten = json.loads(roh)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in daten if x] if isinstance(daten, list) else []


def merke_gesehene(db: Session, store_id: str, ids) -> int:
    """Durchgesehene IDs an den Fortsetzungspunkt anhaengen. Gibt den neuen Stand.

    Reihenfolge bleibt erhalten (kein ``set``): Der Shop liefert nach
    Bestsellern sortiert, und diese Reihenfolge ist die Information - sie sagt,
    wo der naechste Lauf ansetzt.
    """
    from app.services.app_settings import set_app_setting

    if db is None or not store_id:
        return 0
    vorhanden = gesehene_ids(db, store_id)
    bekannt = set(vorhanden)
    for i in ids or ():
        s = str(i).strip()
        if s and s not in bekannt:
            vorhanden.append(s)
            bekannt.add(s)
    set_app_setting(db, f"{_GESEHEN_SCHLUESSEL}{store_id}",
                    json.dumps(vorhanden, ensure_ascii=False))
    return len(vorhanden)


def setze_fortschritt_zurueck(db: Session, store_id: str) -> None:
    """Wieder bei Platz 1 anfangen - fuer einen bewusst frischen Durchgang."""
    from app.services.app_settings import set_app_setting

    if db is None or not store_id:
        return
    set_app_setting(db, f"{_GESEHEN_SCHLUESSEL}{store_id}", None)


async def import_store_products(db: Session, *, store_url_or_id: str, limit: int = 25,
                                bestseller: bool = True,
                                product_ids: list[str] | None = None,
                                fortsetzen: bool = True) -> dict:
    """Bestseller eines Shops als Entwuerfe anlegen. Gibt die Zusammenfassung zurueck.

    ``product_ids``: direkt eingefuegte Produkt-Links/IDs statt Seiten-Ernte —
    der Rest des Ablaufs (Duplikat-Check, Entwurf je Produkt) ist identisch."""
    if product_ids:
        store_id = "ID-Import"
    else:
        store_id = store.extract_store_id(store_url_or_id)
        if not store_id:
            raise PersistentError(
                "Keine Shop-Nummer im Link erkannt. Bitte den Link der AliExpress-SHOP-Seite "
                "einfuegen (z.B. https://de.aliexpress.com/store/1103573332).")
    n = max(1, min(int(limit or 25), MAX_PRODUCTS))

    _state.update({"running": True, "store_id": store_id, "total": 0, "done": 0,
                   "created": 0, "skipped": 0, "failed": 0, "error": None, "letzte": [],
                   "gefunden": 0,
                   # Ein NEUER Lauf erbt nie einen alten Abbrechen-Wunsch (sonst
                   # stirbt z.B. der Auto-Resume nach Deploy-Neustart sofort).
                   "abbruch": False, "abgebrochen": False,
                   # Wiederanlauf-Infos: damit startup_check() nach einem Deploy-
                   # Neustart AUTOMATISCH fortsetzen kann (Nutzerfrage 15.08.).
                   "url": store_url_or_id, "limit": n,
                   "product_ids": list(product_ids) if product_ids else None})
    # Groesseren Vorrat holen, damit am Ende wirklich ``n`` NEUE Produkte dabei sind: schon
    # vorhandene Artikel werden uebersprungen, wuerden sonst aber die Anzahl auffressen.
    # FORTSETZUNGSPUNKT: Ein zweiter Lauf desselben Shops soll dort weitermachen,
    # wo der erste aufhoerte - 1-100, dann 101-200, dann 201-300 (Nutzerwunsch
    # 05.09.2026). Dafuer muss der Vorrat um die schon gesehenen Artikel wachsen,
    # sonst scrollt der Scraper immer wieder durch dieselben ersten Plaetze.
    gesehen = gesehene_ids(db, store_id) if (fortsetzen and not product_ids) else []
    if gesehen:
        pool = min(len(gesehen) + max(n * 3, n + 10), MAX_POOL_FORTSETZUNG)
        logger.info("Shop %s: setze fort, %s Artikel bereits durchgesehen "
                    "(Vorrat %s)", store_id, len(gesehen), pool)
    else:
        pool = min(max(n * 3, n + 10), MAX_POOL)

    if product_ids:
        ids = [str(p).strip() for p in product_ids if str(p).strip()][:MAX_POOL]
    else:
        try:
            ids = await store.fetch_store_product_ids(store_id, limit=pool, bestseller=bestseller)
        except store.StoreScrapeError as exc:
            _state.update({"running": False, "error": str(exc)})
            _speichern()
            raise PersistentError(str(exc)) from exc

    if gesehen:
        bekannt = set(gesehen)
        vorher = len(ids)
        ids = [i for i in ids if str(i) not in bekannt]
        _state["uebersprungen_alt"] = vorher - len(ids)
        logger.info("Shop %s: %s bereits gesehene Artikel uebersprungen, "
                    "%s neue im Vorrat", store_id, vorher - len(ids), len(ids))

    _state["gefunden"] = len(ids)
    _state["total"] = n
    _speichern()
    from app.services import product_service
    created, skipped, failed = [], [], []
    verarbeitet: list[str] = []          # fuer den Fortsetzungspunkt
    for pid in ids:
        if len(created) >= n:
            break
        # JEDER angefasste Artikel zaehlt als durchgesehen - auch ein
        # uebersprungener oder gescheiterter. Sonst bekaeme der naechste Lauf
        # genau die Artikel wieder vorgesetzt, die schon einmal nicht gingen.
        verarbeitet.append(str(pid))
        # Abbruch bei Dauerfehlern. Ohne diese Bremse laeuft der Vorrat komplett
        # durch, wenn etwas Grundsaetzliches nicht stimmt - ein abgelaufener
        # AliExpress-Zugang zum Beispiel. Beim Testlauf am 27.08. waren das
        # dreissig identische Fehlermeldungen: eine Minute vertan, und der
        # eigentliche Grund ging in der Wiederholung unter.
        #
        # Nur wenn NOCH NICHTS geklappt hat: Einzelne kaputte Produkte gibt es
        # immer, die sollen den Lauf nicht stoppen. Sind aber die ersten
        # _ABBRUCH_NACH allesamt gescheitert, liegt es nicht am Produkt.
        if not created and len(failed) >= _ABBRUCH_NACH:
            grund = (failed[-1].get("fehler") or "")[:200]
            _state.update({"error": (
                f"Abgebrochen nach {len(failed)} Fehlversuchen in Folge - kein "
                f"einziges Produkt konnte geladen werden. Letzter Grund: {grund}")})
            logger.warning("store import: Dauerfehler, abgebrochen",
                           extra={"versuche": len(failed)})
            break
        if _state.get("abbruch"):          # Knopf „Abbrechen" im Dashboard
            _state["abgebrochen"] = True
            logger.info("store import abgebrochen", extra={"angelegt": len(created)})
            break
        # Schon im System? Dann gar nicht erst anfassen – kein Scrape, keine KI, kein Doppel.
        vorhanden = existing_product(db, pid)
        if vorhanden is not None:
            skipped.append({"produkt_id": pid, "grund": f"schon im System (Produkt #{vorhanden.id})",
                            "produkt_db_id": vorhanden.id})
            _state["skipped"] = len(skipped)
            _state["done"] = len(created) + len(failed)
            continue
        url = store.product_url(pid)
        try:
            res = await product_service.upload_product(db, aliexpress_url=url)
            # Warnungen mitnehmen. upload_product meldet z.B. "Titel auf 80 Zeichen
            # gekuerzt" oder einen Marken-Hinweis. Bei EINEM Produkt sieht der Mensch
            # das in der Antwort - bei einem Shop-Import mit 50 Stueck verschwanden
            # sie bisher restlos, obwohl gerade dort niemand jeden Titel einzeln liest.
            # Testlauf 27.08.: zwei von zehn Titeln waren abgeschnitten, einer davon
            # mit offenem Anfuehrungszeichen. Gemeldet hatte es das System - gehoert
            # hat es niemand.
            warnungen = [w for w in (res.get("warnings") or []) if w]
            created.append({"produkt_id": pid, "listing_id": res.get("listing_id"),
                            "titel": (res.get("title_seo") or "")[:70],
                            "warnungen": warnungen})
            _state["created"] = len(created)
            _state["letzte"] = [c["titel"] for c in created[-3:]]
        except PersistentError as exc:
            # Sicherheitsnetz: Duplikate faengt schon die Pruefung oben ab, aber der Upload
            # kennt weitere Ablehnungsgruende (z.B. Produkt nicht mehr verfuegbar).
            skipped.append({"produkt_id": pid, "grund": str(exc)[:150]})
            _state["skipped"] = len(skipped)
        except Exception as exc:  # noqa: BLE001 – ein Produkt darf den Lauf nie stoppen
            logger.warning("store import item failed",
                           extra={"produkt": pid, "error": str(exc)[:200]})
            failed.append({"produkt_id": pid, "fehler": str(exc)[:200]})
            _state["failed"] = len(failed)
        _state["done"] = len(created) + len(failed)
        _speichern()
        await asyncio.sleep(_PAUSE_S)

    _state["running"] = False
    _state["abbruch"] = False           # Wunsch ist erledigt, Kennzeichen bleibt
    _speichern()

    # Fortsetzungspunkt fortschreiben. Auch nach einem Abbruch: was durchgesehen
    # wurde, ist durchgesehen - beim naechsten Mal soll der Lauf weiter vorn
    # ansetzen, nicht wieder von Platz 1.
    stand = 0
    if verarbeitet and not product_ids:
        try:
            stand = merke_gesehene(db, store_id, verarbeitet)
        except Exception as exc:  # noqa: BLE001 - darf das Ergebnis nie kippen
            logger.warning("Fortsetzungspunkt nicht gespeichert: %s", str(exc)[:150])

    logger.info("store import fertig", extra={"store": store_id, "angelegt": len(created),
                "uebersprungen": len(skipped), "fehler": len(failed),
                "durchgesehen_gesamt": stand})
    mit_warnung = [c for c in created if c.get("warnungen")]
    if mit_warnung:
        logger.info("store import: Produkte mit Hinweisen",
                    extra={"anzahl": len(mit_warnung), "von": len(created)})
    return {"store_id": store_id, "gefunden": len(ids), "angelegt": created,
            "uebersprungen": skipped, "fehlgeschlagen": failed,
            "n_angelegt": len(created), "n_uebersprungen": len(skipped),
            "n_fehlgeschlagen": len(failed),
            # Damit die Oberflaeche einen Grund hat, hinzusehen: wieviele der neuen
            # Entwuerfe brauchen einen Blick, bevor sie live gehen.
            "n_mit_hinweis": len(mit_warnung),
            # Wo der naechste Lauf ansetzt. Ohne diese Zahl sieht der Betreiber
            # nicht, ob er noch weitersuchen kann oder der Shop durch ist.
            "durchgesehen_gesamt": stand,
            "uebersprungen_alt": _state.get("uebersprungen_alt", 0)}


async def import_store_products_bg(*, store_url_or_id: str, limit: int = 25,
                                   product_ids: list[str] | None = None) -> None:
    """Hintergrund-Lauf mit eigener DB-Session (der HTTP-Request kehrt sofort zurueck)."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        await import_store_products(db, store_url_or_id=store_url_or_id, limit=limit,
                                    product_ids=product_ids)
    except Exception as exc:  # noqa: BLE001
        _state.update({"running": False, "error": str(exc)[:250]})
        _speichern()
        logger.warning("store import bg failed", extra={"error": str(exc)[:200]})
    finally:
        db.close()
