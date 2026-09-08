"""Sanitisierter Dump der letzten Bestell-Fehler (Diagnose via GitHub Actions).

Wird vom manuellen Workflow ``diagnose.yml`` auf dem VPS ausgefuehrt (read-only).
Die Ausgabe enthaelt BEWUSST KEINE Kaeuferdaten (keine Namen, Strassen, Mails) —
nur sale_id, Zeitpunkt, Zielland, Sale-Status und den Fehlertext des
fulfill-Laufs. Damit laesst sich ein "Bestellung X funktioniert nicht" ohne
Dashboard-Zugriff diagnostizieren.
"""
from __future__ import annotations

import os
import sys

# Repo-Root in den Modulpfad (Aufruf ist "python scripts/dump_order_errors.py").
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Sale, TaskLog


def sale_detail(sale_id: int) -> None:
    """Bestell-relevante Daten EINES Sales (sanitisiert: keine Namen/Strassen/Mails)."""
    from app.models import Listing, Product
    db = SessionLocal()
    try:
        sale = db.get(Sale, sale_id)
        if sale is None:
            print(f"sale {sale_id}: nicht gefunden")
            return
        listing = db.get(Listing, sale.listing_id) if sale.listing_id else None
        product = (db.get(Product, listing.product_id)
                   if listing is not None and listing.product_id else None)
        addr = sale.delivery_address or {}
        print(f"sale {sale_id}: status={sale.status} qty={sale.quantity} "
              f"land={addr.get('country')}")
        # Fuer Adress-Diagnosen (ohne Name/Strasse/Mail): Ort/PLZ/Bundesland
        print(f"  ort='{addr.get('city')}' plz='{addr.get('postal') or addr.get('postcode')}' "
              f"bundesland='{addr.get('province') or addr.get('state')}'")
        # Feld-Inventar OHNE Inhalte: welche Felder existieren, wie lang sind sie?
        import re as _re
        inv = {k: len(str(v)) for k, v in addr.items() if v not in (None, "")}
        phone_digits = len(_re.sub(r"\D", "", str(addr.get("phone") or addr.get("mobile") or "")))
        print(f"  feld_laengen={inv} phone_ziffern={phone_digits}")
        print(f"  variant_selected={sale.variant_selected}")
        try:
            from app.models import OrderAliexpress
            rows = db.scalars(select(OrderAliexpress)
                              .where(OrderAliexpress.sale_id == sale.id)).all()
            print("  ali_orders=" + (" | ".join(
                f"{getattr(r, 'aliexpress_order_id', '?')}({getattr(r, 'status', '?')})"
                for r in rows) or "-"))
        except Exception as exc:  # noqa: BLE001
            print(f"  ali_orders=? ({str(exc)[:80]})")
        if listing is not None:
            print(f"  listing={listing.id} '{(listing.title_seo or '')[:70]}'")
            print(f"  variant_map={dict(list((listing.variant_map or {}).items())[:10])}")
            print(f"  variant_source_map={listing.variant_source_map}")
        if product is not None:
            print(f"  product_ali_id={product.aliexpress_id}")
            skus = (product.variants or {}).get("skus") or []
            print(f"  haupt_skus={len(skus)}: "
                  + " | ".join(str(s.get("attr")) for s in skus[:15]))
            slots = (product.alternatives or {}).get("sources") or []
            print("  slots=" + " | ".join(
                f"{s.get('aliexpress_id')}({len(s.get('skus') or [])} skus)" for s in slots))
    finally:
        db.close()


def order_detail_raw(order_id: str) -> None:
    """Adress-Schluessel einer EIGENEN AliExpress-Bestellung (read-only).

    Zweck (Sale 1256): die von AliExpress AKZEPTIERTE Stadt-/Provinz-Schreibweise
    einer manuell aufgegebenen Bestellung ablesen. Strassen-/Namensfelder werden
    nur als Laenge gezeigt."""
    import asyncio
    import json as _json
    from app.config import get_settings
    from app.integrations.aliexpress import RealAliExpressClient
    c = RealAliExpressClient(get_settings())
    data = asyncio.run(c._call("aliexpress.trade.ds.order.get", {
        "single_order_query": _json.dumps({"order_id": str(order_id)})}))
    addr_keys = ("city", "province", "state", "country", "address", "zip", "postal", "contact")
    show = ("city", "province", "state", "country", "zip", "postal")

    def walk(o, path=""):
        if isinstance(o, dict):
            for k, v in o.items():
                kl = str(k).lower()
                if any(a in kl for a in addr_keys) and not isinstance(v, (dict, list)):
                    ok = any(x in kl for x in show) and "address" not in kl and "contact" not in kl
                    print(f"  {path}{k} = " + (repr(v) if ok else f"<{len(str(v))} Zeichen>"))
                else:
                    walk(v, f"{path}{k}.")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}{i}.")
    walk(data)
    print("[fertig]")


def find_listing(text: str) -> None:
    """Read-only: aktive Listings per Titel-Teil finden (id/status/item)."""
    from app.models import Listing
    db = SessionLocal()
    try:
        rows = db.scalars(select(Listing)
                          .where(Listing.title_seo.ilike(f"%{text}%"))).all()
        for l in rows:
            print(f"  listing={l.id} status={l.listing_status} item={l.ebay_item_id} "
                  f"grp={l.ebay_draft_id} '{(l.title_seo or '')[:70]}'")
        print(f"[{len(rows)} Treffer]")
    finally:
        db.close()


def names_preview(listing_id: int) -> None:
    """Read-only VORSCHAU der Namens-Reparatur: Live-Gruppe vs. lokale Roh-Namen."""
    import asyncio
    from app.models import Listing, Product
    from app.services import golive_service as gl
    db = SessionLocal()
    try:
        l = db.get(Listing, listing_id)
        p = db.get(Product, l.product_id) if l and l.product_id else None
        axis_names, variants = gl._usable_variants(p)
        grp = asyncio.run(gl._real_ebay().get_inventory_item_group(l.ebay_draft_id))
        varies = (grp or {}).get("variesBy") or {}
        print(f"  live_specs={varies.get('specifications')}")
        print(f"  live_skus={(grp or {}).get('variantSKUs')}")
        print(f"  achsen={axis_names}")
        for v in variants:
            attr = str(v.get("attr") or "")
            raw = (attr.split("#", 1)[1] if "#" in attr
                   else (v["options"][axis_names[0]] if axis_names else "?"))
            print(f"  lokal attr='{attr}' -> roh='{raw}' stock={v.get('stock')}")
    finally:
        db.close()


def run_append(listing_id: int) -> None:
    """Namens-Reparatur Option A ausfuehren (explizite Nutzer-Anweisung, task_log-auditiert)."""
    import asyncio
    from app.services import golive_service as gl
    db = SessionLocal()
    try:
        res = asyncio.run(gl.append_corrected_variants(db, listing_id=listing_id))
        print(f"  ERGEBNIS: {res}")
    finally:
        db.close()


def main(limit: int = 25) -> None:
    db = SessionLocal()
    try:
        logs = db.scalars(
            select(TaskLog)
            .where(TaskLog.task_type == "fulfill", TaskLog.status == "failed")
            .order_by(TaskLog.id.desc())
            .limit(max(1, limit))).all()
        for tl in logs:
            sale = (db.get(Sale, int(tl.reference_id))
                    if (tl.reference_id or "").isdigit() else None)
            country = ((sale.delivery_address or {}).get("country")
                       if sale is not None else None)
            print(f"--- sale {tl.reference_id} | {tl.created_at} | land={country} "
                  f"| sale_status={(sale.status if sale is not None else '?')}")
            print("    " + (tl.error_message or "(kein Fehlertext)")
                  .strip()[:1600].replace("\n", " | "))
        print(f"[{len(logs)} fehlgeschlagene fulfill-Laeufe]")
    finally:
        db.close()


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "25"
    if arg.startswith("sale:"):
        sale_detail(int(arg.split(":", 1)[1]))
    elif arg.startswith("order:"):
        order_detail_raw(arg.split(":", 1)[1])
    elif arg.startswith("find:"):
        find_listing(arg.split(":", 1)[1])
    elif arg.startswith("names:"):
        names_preview(int(arg.split(":", 1)[1]))
    elif arg.startswith("append:"):
        run_append(int(arg.split(":", 1)[1]))
    elif arg.startswith("zorofix:"):
        # Nutzerauftrag 19.08.: Zoro-Ohrringe (Listing 1284) — "3er-Set" ist
        # FALSCH, es ist EIN Paar je Farbvariante (Gold/Silber). zorofix:<id> =
        # Vorschau (+ Quell-Rohtitel als Ursachen-Beleg); zorofix:<id>:go =
        # Titel+Beschreibung korrigieren, via update_listing_live auch auf eBay.
        import asyncio as _aio
        import re as _re
        from app.models import Listing as _L, Product as _Pr
        _parts = arg.split(":")
        _lid = int(_parts[1])
        _go = len(_parts) > 2 and _parts[2] == "go"
        _TITEL_NEU = ("One Piece Zoro Ohrringe 1 Paar Damen Herren Cosplay "
                      "Gold Silber Anime Manga")
        _ERSATZ = [
            # Kompletter Unsinns-Satz aus der Vorschau ("Vier Öhrchen statt zwei –
            # 3er-Set (insgesamt drei Paare ...)"): ganz ersetzen, nicht flicken.
            (_re.compile(r"Vier Öhrchen statt zwei[^.\n]*\.?", _re.I),
             "Ein Paar Ohrringe im Zoro-Stil mit Dreifach-Optik – wahlweise in "
             "Gold oder Silber."),
            (_re.compile(r"3\s*er[\s-]?Set", _re.I), "1 Paar"),
            (_re.compile(r"Set\s+aus\s+3", _re.I), "1 Paar"),
            (_re.compile(r"\b3\s*Paar(?:e)?\b", _re.I), "1 Paar"),
            (_re.compile(r"\bdrei\s+Paare?\b", _re.I), "ein Paar"),
            (_re.compile(r"\b3\s*(?:Stück|Stk\.?)\b", _re.I), "1 Paar"),
            (_re.compile(r"\b3\s*Ohrringe\b", _re.I), "1 Paar Ohrringe"),
            (_re.compile(r"\bdrei\s+Ohrringe\b", _re.I), "ein Paar Ohrringe")]
        _db = SessionLocal()
        try:
            _l = _db.get(_L, _lid)
            if _l is None:
                print("  Listing nicht gefunden")
            else:
                print(f"  TITEL AKTUELL ({len(_l.title_seo or '')}): {_l.title_seo}")
                print(f"  TITEL NEU     ({len(_TITEL_NEU)}): {_TITEL_NEU}")
                _desc = _l.description or ""
                for _z in _desc.splitlines():
                    if _re.search(r"\b3\b|set|paar|stück", _z, _re.I):
                        print(f"    BESCHR: {_z.strip()[:140]}")
                _p = _db.get(_Pr, _l.product_id) if _l.product_id else None
                if _p is not None and _p.aliexpress_id and not _go:
                    try:
                        from app.integrations import get_aliexpress_client as _gac
                        _ae = _gac()
                        _raw = _aio.run(_ae._call("aliexpress.ds.product.get", {
                            "product_id": str(_p.aliexpress_id),
                            "ship_to_country": "DE", "target_currency": "EUR",
                            "target_language": "de"}))
                        from app.integrations import aliexpress_api as _api
                        _titel_roh = (_api.parse_product(_raw) or {}).get("title_raw")
                        print(f"  QUELLE ROH: {_titel_roh}")
                    except Exception as exc:  # noqa: BLE001
                        print(f"  QUELLE ROH: nicht lesbar ({str(exc)[:80]})")
                if _go:
                    _neu = _desc
                    for _rx, _ers in _ERSATZ:
                        _neu = _rx.sub(_ers, _neu)
                    from app.services import golive_service as _gl
                    _r = _aio.run(_gl.update_listing_live(
                        _db, listing_id=_lid, title=_TITEL_NEU,
                        description=(_neu if _neu != _desc else None)))
                    print(f"  ZOROFIX GO: {_r}")
        finally:
            _db.close()
    elif arg.startswith("research:"):
        # MUTIEREND (Nutzerauftrag 19.08., Nintendo-Suche): die ECHTE Ideen-Suche
        # fuer einen Begriff nachfahren — legt ProductIdea-Zeilen an (wie der
        # "Suchen"-Knopf) und druckt kept/scanned/drops. research:<begriff>:<n>
        import asyncio as _aio
        from app.services import product_research_service as _r
        _rest = arg.split(":", 1)[1]
        if ":" in _rest and _rest.rsplit(":", 1)[1].isdigit():
            _begriff, _n = _rest.rsplit(":", 1)[0], int(_rest.rsplit(":", 1)[1])
        else:
            _begriff, _n = _rest, 25
        if not _r.try_acquire_run():
            print("  UEBERSPRUNGEN: eine Suche laeuft bereits")
        else:
            _db = SessionLocal()
            try:
                _res = _aio.run(_r.discover(_db, niches=[_begriff], target=_n))
                print(f"  RESEARCH '{_begriff}' (Ziel {_n}): kept={_res['kept']} "
                      f"scanned={_res['scanned']} elektro={_res['electronics_skipped']} "
                      f"ohne_eu_lager={_res['local_skipped']} drops={_res['drops']}")
            finally:
                _db.close()
                _r.release_run()
    elif arg.startswith("suchprobe:"):
        # DIAGNOSE (nur lesen, 19.08.): warum liefert eine Ideen-Suche 0 Treffer?
        # Zeigt, was die API-SUCHE fuer den Begriff hergibt und wie die ersten
        # Kandidaten durch die Filter laufen wuerden. KEINE DB-Schreibvorgaenge.
        # Nutzung: suchprobe:<begriff>
        import asyncio as _aio
        from app.integrations import get_aliexpress_client as _gac
        from app.services import product_research_service as _r
        _begriff = arg.split(":", 1)[1]
        _ae = _gac()

        async def _probe():
            cands = await _r._text_search(_ae, _begriff, page_size=25)
            print(f"  SUCHPROBE '{_begriff}': {len(cands)} Roh-Kandidaten der API-Suche")
            for c in cands[:6]:
                print(f"    id={c['id']} score={c['score']} preis={c['price']} "
                      f"'{(c.get('title') or '')[:70]}'")
            for c in cands[:3]:
                try:
                    e = await _r._enrich(_ae, c["id"])
                except Exception as exc:  # noqa: BLE001
                    print(f"    enrich {c['id']}: FEHLER {str(exc)[:120]}")
                    continue
                _el = _r._is_electronic(e.get("title")) or _r._is_electronic(c.get("title"))
                print(f"    enrich {c['id']}: rating={e['rating']} reviews={e['reviews']} "
                      f"liefertage={e['delivery_days']} status={e['status']} "
                      f"choice={e['sl_product']} ships={e['ships_from']} "
                      f"preis_cny={e['price_cny']} elektro={_el}")
                await _aio.sleep(1.2)
        try:
            _aio.run(_probe())
        except Exception as exc:  # noqa: BLE001
            print(f"  FEHLER: {type(exc).__name__}: {str(exc)[:300]}")
    elif arg.startswith("storestart:"):
        # FERNSTART des Shop-Imports (Nutzerauftrag 19.08.: ">=100 Entwuerfe aus
        # Store 1105317951"): schreibt NUR die Wiederanlauf-Datei — beim naechsten
        # Dienst-Neustart (Deploy) setzt startup_check() den Lauf IM SERVER fort,
        # mit Dashboard-Fortschritt und Abbrechen-Knopf wie beim Klick-Start.
        # Nutzung: storestart:<store_url_or_id>:<anzahl>
        import json as _json
        from app.services import store_import_service as _sis
        _rest = arg.split(":", 1)[1]
        if ":" in _rest and _rest.rsplit(":", 1)[1].isdigit():
            _ziel_url, _n = _rest.rsplit(":", 1)[0], int(_rest.rsplit(":", 1)[1])
        else:
            _ziel_url, _n = _rest, 100
        _datei = _sis._state_datei()
        try:
            _alt = _json.loads(_datei.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _alt = {}
        if isinstance(_alt, dict) and _alt.get("running"):
            print("  UEBERSPRUNGEN: laut Zustandsdatei laeuft bereits ein Import")
        else:
            _datei.write_text(_json.dumps({
                "running": True, "store_id": None, "total": 0, "done": 0,
                "created": 0, "skipped": 0, "failed": 0, "error": None,
                "letzte": [], "gefunden": 0, "abbruch": False,
                "abgebrochen": False, "url": _ziel_url, "limit": _n,
                "product_ids": None}, ensure_ascii=False), encoding="utf-8")
            print(f"  STORESTART vorgemerkt: url={_ziel_url} limit={_n} — "
                  f"startet beim naechsten Dienst-Neustart (Deploy)")
    elif arg == "storestatus":
        # Read-only: Fortschritt des Shop-Imports aus der Zustandsdatei (die der
        # Server-Prozess bei jedem Schritt aktualisiert).
        import json as _json
        from app.services import store_import_service as _sis
        try:
            _s = _json.loads(_sis._state_datei().read_text(encoding="utf-8"))
            print("  STORESTATUS: " + _json.dumps(
                {k: _s.get(k) for k in ("running", "store_id", "gefunden",
                                        "total", "done", "created", "skipped",
                                        "failed", "error", "abgebrochen")},
                ensure_ascii=False))
            for _t in (_s.get("letzte") or [])[-3:]:
                print(f"    zuletzt: {_t}")
        except (OSError, ValueError) as exc:
            print(f"  STORESTATUS: Zustandsdatei nicht lesbar ({exc})")
    elif arg.startswith("storeharvest:"):
        # DIAGNOSE (nur lesen, 19.08.): die NEUE Ansichten-Ernte eines Stores
        # messen — wie viele IDs liefern Sortierungen + Preisbaender wirklich?
        # KEIN Import, keine Entwuerfe. Nutzung: storeharvest:<store_id>[:<ziel>]
        import asyncio as _aio
        from app.integrations import aliexpress_store as _store
        _parts = arg.split(":")
        _sid = _parts[1]
        _n = int(_parts[2]) if len(_parts) > 2 else 150
        _prot: list = []
        try:
            _ids = _aio.run(_store.fetch_store_product_ids(
                _sid, limit=_n, protokoll=_prot))
            print(f"  STOREHARVEST {_sid}: {len(_ids)} IDs (Ziel {_n})")
            for _e in _prot:
                print(f"    {_e['ansicht']}: geerntet={_e['geerntet']} neu={_e['neu']}")
        except Exception as exc:  # noqa: BLE001
            print(f"  FEHLER: {type(exc).__name__}: {str(exc)[:300]}")
    elif arg.startswith("mtopprobe:"):
        # DIAGNOSE (nur lesen, 19.08.): laesst sich die interne Store-Listen-API
        # (mtop recommend) aus dem Seitenkontext weiterblaettern? Fuer den
        # 100er-Import (alle anderen Blaetter-Wege sind nachweislich tot).
        import asyncio as _aio
        import json as _json
        from app.integrations import aliexpress_store as _store
        try:
            _r = _aio.run(_store.probe_mtop(arg.split(":", 1)[1]))
            print("  MTOPPROBE: " + _json.dumps(_r, ensure_ascii=False)[:2500])
        except Exception as exc:  # noqa: BLE001
            print(f"  FEHLER: {type(exc).__name__}: {str(exc)[:300]}")
    elif arg.startswith("pageprobe:"):
        # DIAGNOSE (nur lesen, 19.08.): Blaetter-Mechanik einer AliExpress-Seite
        # ausmessen — fuer den 100er-Store-Import (Ernte blieb bei ~40 haengen).
        import asyncio as _aio
        import json as _json
        from app.integrations import aliexpress_store as _store
        _url = arg.split(":", 1)[1]
        try:
            _r = _aio.run(_store.probe_page(_url))
            print("  PAGEPROBE: " + _json.dumps(_r, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            print(f"  FEHLER: {type(exc).__name__}: {str(exc)[:300]}")
    elif arg.startswith("harvest:"):
        import asyncio as _aio
        from app.integrations import aliexpress_store as _store
        from app.services.product_research_service import _local_search_url
        raw = arg.split(":", 1)[1]
        url = raw if raw.startswith("http") else _local_search_url(raw)
        print(f"  url={url}")
        try:
            ids = _aio.run(_store.fetch_page_product_ids(url, limit=120, max_scrolls=30))
            print(f"  ids={len(ids)}: {ids[:8]}")
        except Exception as exc:  # noqa: BLE001
            print(f"  FEHLER: {type(exc).__name__}: {str(exc)[:300]}")
    elif arg.startswith("resync:"):
        import asyncio as _aio
        from app.services import monitoring_service as _mon
        parts = arg.split(":")
        _lid = int(parts[1])
        _db = SessionLocal()
        try:
            if len(parts) > 2 and parts[2] == "reset":
                # Spiegel leeren: ein frueherer Aggregat-"Erfolg" kann ihn vergiftet
                # haben (Listing 200) -> danach rechnet der Resync alles frisch und
                # pusht wirklich je Variation.
                from sqlalchemy.orm.attributes import flag_modified as _fm
                from app.models import Listing as _L
                _l = _db.get(_L, _lid)
                _l.variant_stock = None
                _fm(_l, "variant_stock")
                _db.commit()
                print("  Spiegel geleert")
            _r = _aio.run(_mon.resync_listing_variant_stock(_db, listing_id=_lid))
            print(f"  RESYNC: {_r}")
        finally:
            _db.close()
    elif arg.startswith("automap:"):
        import asyncio as _aio
        from app.services import golive_service as _gl
        _db = SessionLocal()
        try:
            _r = _aio.run(_gl.auto_map_ebay_variations(
                _db, listing_id=int(arg.split(":", 1)[1])))
            print(f"  AUTOMAP: {_r}")
        finally:
            _db.close()
    elif arg == "banksync":
        import asyncio as _aio
        from app.services import bank_sync_service as _bank
        _db = SessionLocal()
        try:
            _r1 = _aio.run(_bank.sync_bank_transactions(_db))
            _r2 = _bank.match_aliexpress_orders(_db)
            _r3 = _aio.run(_bank.sync_ebay_payouts(_db))
            _r4 = _aio.run(_bank.archive_bank_csv(_db))
            from app.services import kontierung_service as _kont
            _rk = _kont.kontiere_neue(_db)
            _rec = _bank.bank_reconciliation(_db)
            print(f"  SYNC: {_r1}  MATCH: {_r2}")
            print(f"  PAYOUTS: {_r3}")
            print(f"  ARCHIV: {_r4}")
            print(f"  KONTIERUNG: {_rk}")
            print(f"  OFFEN: abbuchungen={len(_rec['ali_open'])} "
                  f"bestell_gruppen={_rec['orders_unmatched_total']} "
                  f"erstattungen={len(_rec['refunds'])}")
        finally:
            _db.close()
    elif arg == "tracksync":
        # MUTIEREND (Nutzer-Anweisung 19.08. "nachziehen"): den normalen 45-min-
        # Tracking-Sync EINMAL sofort ausfuehren (holt Nummern + meldet an eBay —
        # exakt derselbe Pfad wie der Scheduler-Job, keine Sonderlogik).
        import asyncio as _aio
        from app.services import order_service as _os
        _db = SessionLocal()
        try:
            _r = _aio.run(_os.sync_tracking_all(_db))
            print(f"  TRACKSYNC: offen={_r['orders_open']} geprueft={_r['checked']} "
                  f"an_ebay_gemeldet={_r['reported_to_ebay']} fehler={_r['errors']}")
        finally:
            _db.close()
    elif arg.startswith("track:"):
        # READ-ONLY: rohe AliExpress-Tracking-Antwort je Sale + unser Final-Urteil
        # (Vorfall 19.08.: Auslands-Sales 1266/1274/1277 ohne Sendungsnummer —
        # Verdacht: _is_final_tracking kennt nur DE-Endzusteller). KEIN DB-Write.
        import asyncio as _aio
        from app.integrations import get_aliexpress_client as _gac
        from app.models import OrderAliexpress as _O, Sale as _S
        from app.services.order_service import _is_final_tracking, carrier_from_tracking
        _db = SessionLocal()
        _ae = _gac()

        async def _probe(_ids):
            for _sid in _ids:
                _s = _db.get(_S, _sid)
                _o = (_db.query(_O).filter(_O.sale_id == _sid).first()
                      if _s is not None else None)
                if _s is None or _o is None:
                    print(f"  sale={_sid}: kein Sale/keine Order")
                    continue
                _land = str(((_s.delivery_address or {}).get("country")) or "?")
                try:
                    _t = await _ae.get_tracking(_o.aliexpress_order_id or "")
                    _num = _t.tracking_number
                    print(f"  sale={_sid} land={_land} ae_order={_o.aliexpress_order_id} "
                          f"order_date={str(_o.order_date)[:10]} gespeichert="
                          f"{_o.tracking_number or '-'}")
                    print(f"    API: nummer={_num or '-'} carrier={_t.carrier or '-'} "
                          f"status={(_t.status or '-')[:60]}")
                    print(f"    Urteil: erkannt={carrier_from_tracking(_num) or '-'} "
                          f"final={_is_final_tracking(_num)}")
                except Exception as _exc:  # noqa: BLE001
                    print(f"  sale={_sid} land={_land}: Abruf-FEHLER {str(_exc)[:160]}")
        try:
            _aio.run(_probe([int(t) for t in arg.split(":", 1)[1].split(",") if t.strip()]))
        finally:
            _db.close()
    elif arg == "landscan":
        # READ-ONLY: Bestands-Scan Laender-Ausschluss (16.08.) — welche aktiven
        # EU-Lager-Listings bieten CH/GB an, ohne dass die Quelle dorthin liefert?
        # Schreibt NUR die Vorschlagsliste (data/country_exclusion_state.json).
        import asyncio as _aio
        from app.config import get_settings as _gs
        from app.integrations import get_aliexpress_client as _gac
        from app.integrations.ebay import RealEbayClient as _REC
        from app.services import listing_country_service as _lcs
        _db = SessionLocal()
        try:
            _r = _aio.run(_lcs.scan(_db, ebay=_REC(_gs()), ae=_gac()))
            print(f"  LANDSCAN: geprueft={_r['kandidaten_geprueft']} "
                  f"vorschlaege={_r['vorschlaege']} policy_sicher={_r['policy_sicher']} "
                  f"klassik={_r['klassik_ohne_offer']}")
            for _v in _r["liste"]:
                print(f"    listing={_v['listing_id']} laender={_v['laender']} "
                      f"policy='{_v['policy_name'][:30]}' '{_v['titel'][:55]}'")
        finally:
            _db.close()
    elif arg.startswith("landapply:"):
        # MUTIEREND (nur auf ausdrueckliche Nutzer-Freigabe je Liste): stellt die
        # freigegebenen Listings auf die Policy-VARIANTE mit Laender-Ausschluss um.
        # Bestehende Policies werden NIE geaendert (Kopie je Ausschluss-Muster).
        import asyncio as _aio
        from app.config import get_settings as _gs
        from app.integrations.ebay import RealEbayClient as _REC
        from app.services import listing_country_service as _lcs
        _ids = [int(t) for t in arg.split(":", 1)[1].split(",") if t.strip()]
        _db = SessionLocal()
        try:
            _r = _aio.run(_lcs.apply(_db, ebay=_REC(_gs()), listing_ids=_ids))
            print(f"  LANDAPPLY: umgestellt={_r['umgestellt']}/{len(_ids)}")
            for _e in _r["ergebnisse"]:
                print(f"    listing={_e['listing_id']} "
                      f"{'OK laender=' + str(_e.get('laender')) + ' offers=' + str(_e.get('offers')) if _e.get('ok') else 'FEHLER: ' + str(_e.get('note'))[:100]}")
        finally:
            _db.close()
    elif arg.startswith("nurde:"):
        # Versand EINES Listings auf "nur Deutschland" begrenzen (19.08.,
        # Temu-Powerbank Listing 248 — Quelle liefert nur innerhalb DE).
        # nurde:<listing_id> = READ-ONLY-Vorschau; nurde:<listing_id>:go =
        # umstellen. Es wird eine Policy-KOPIE "<Name> nur DE" angelegt und nur
        # dem einen Listing zugewiesen — bestehende Policies bleiben unangetastet.
        import asyncio as _aio
        from app.config import get_settings as _gs
        from app.integrations.ebay import RealEbayClient as _REC
        from app.services import listing_country_service as _lcs
        _parts = arg.split(":")
        _go = len(_parts) > 2 and _parts[2] == "go"
        _db = SessionLocal()
        try:
            _r = _aio.run(_lcs.nur_deutschland(
                _db, ebay=_REC(_gs()), listing_id=int(_parts[1]), go=_go))
            print(f"  NURDE {'GO' if _go else 'VORSCHAU'}: {_r}")
        finally:
            _db.close()
    elif arg.startswith("land:"):
        # Read-only: Verkaeufe nach LIEFERLAND (z.B. land:EE) — id, Status, Datum,
        # Artikel. Fuer die Aufklaerung historischer Auslands-Faelle (16.08.).
        from app.models import Listing as _L, Sale as _S
        _code = arg.split(":", 1)[1].strip().upper()
        _db = SessionLocal()
        try:
            _rows = _db.query(_S).order_by(_S.id.desc()).limit(2000).all()
            _hits = [s for s in _rows
                     if str(((s.delivery_address or {}).get("country")) or "").upper() == _code]
            print(f"  {len(_hits)} Verkaeufe nach {_code}:")
            for s in _hits[:20]:
                _l = _db.get(_L, s.listing_id) if s.listing_id else None
                _d = (s.sale_date or s.created_at)
                print(f"    sale={s.id} status={s.status} datum={str(_d)[:10]} "
                      f"'{((_l.title_seo if _l else None) or '?')[:60]}'")
        finally:
            _db.close()
    elif arg == "policies":
        # Read-only Machbarkeits-Probe (16.08.): Versand-Policies inkl. shipToLocations
        # (regionIncluded/regionExcluded) — kann eBay Laender JE POLICY ausschliessen,
        # und wie sieht die Struktur konkret aus? KEINE Schreib-Calls.
        import asyncio as _aio
        from app.config import get_settings as _gs
        from app.integrations.ebay import RealEbayClient as _REC
        _ec = _REC(_gs())

        async def _dump():
            pols = await _ec.list_fulfillment_policies()
            print(f"  {len(pols)} Versand-Policies:")
            for p in pols:
                raw = await _ec._get_json(
                    f"{_ec._account}/fulfillment_policy/{p['policy_id']}")
                stl = (raw or {}).get("shipToLocations") or {}
                inc = [r.get("regionName") for r in (stl.get("regionIncluded") or [])]
                exc = [r.get("regionName") for r in (stl.get("regionExcluded") or [])]
                print(f"    {p['policy_id']} '{(p.get('name') or '')[:40]}' "
                      f"include={inc} exclude={exc}")
        _aio.run(_dump())
    elif arg.startswith("shipto:"):
        # Read-only: Liefer-Check-Kalibrierung — liefert Produkt X ins Land Y?
        # Nutzung: shipto:<aliexpress_product_id>:<laendercode>  (z.B. shipto:1005..:AT)
        import asyncio as _aio
        from app.integrations import get_aliexpress_client as _gac
        _parts = arg.split(":")
        _pid, _land = _parts[1], (_parts[2] if len(_parts) > 2 else "AT").upper()
        _ae = _gac()
        _v = _aio.run(_ae.delivery_availability(product_id=_pid, country=_land))
        print(f"  SHIPTO {_pid} -> {_land}: "
              f"{'LIEFERT' if _v is True else ('LIEFERT NICHT' if _v is False else 'UNBEKANNT')} "
              f"(client={type(_ae).__name__})")
    elif arg.startswith("ideas:"):
        # Read-only: Produkt-Ideen auflisten, deren Nische ODER Titel den Begriff
        # enthaelt (z.B. "ideas:🏬" = alle Store-Funde). Fuer Filter-Diagnosen.
        from app.models import ProductIdea as _PI
        _q = arg.split(":", 1)[1].strip()
        _db = SessionLocal()
        try:
            _rows = _db.query(_PI).order_by(_PI.id.desc()).limit(400).all()
            _hits = [x for x in _rows
                     if _q.lower() in (x.niche or "").lower()
                     or _q.lower() in (x.title or "").lower()]
            print(f"  {len(_hits)} Ideen zu '{_q}':")
            for x in _hits:
                print(f"    idea={x.id} status={x.status} nische='{(x.niche or '')[:36]}' "
                      f"titel='{(x.title or '')[:80]}'")
        finally:
            _db.close()
    elif arg.startswith("delideas:"):
        # MUTIEREND (nur auf konkrete Nutzer-Meldung): Produkt-IDEEN loeschen —
        # betrifft AUSSCHLIESSLICH Vorschlags-Zeilen (product_ideas), NIE Listings,
        # Produkte oder eBay (Vorfall 16.08.: Elektronik in Store-Funden).
        from app.models import ProductIdea as _PI
        _db = SessionLocal()
        try:
            for _tok in arg.split(":", 1)[1].split(","):
                _tok = _tok.strip()
                if not _tok:
                    continue
                _x = _db.get(_PI, int(_tok))
                if _x is None:
                    print(f"    idea={_tok}: nicht gefunden")
                    continue
                print(f"    geloescht idea={_x.id} '{(_x.title or '')[:70]}'")
                _db.delete(_x)
            _db.commit()
        finally:
            _db.close()
    elif arg == "storedaily":
        # Taegliche Store-Entdeckung EINMAL manuell anstossen (gleicher Pfad wie der
        # 05:10-Job). Schreibt 🏬-Ideen + Merkliste data/store_ideas_seen.json.
        import asyncio as _aio
        from app.services import product_research_service as _research
        if not _research.try_acquire_run():
            print("  UEBERSPRUNGEN: eine Suche laeuft bereits")
        else:
            # ACHTUNG: der Lauf-Lock ist PROZESS-lokal — er schuetzt nicht gegen
            # die Scheduler-Jobs des laufenden Servers. Nicht zwischen 04:00 und
            # 05:30 UTC ausfuehren (Trend-/Store-Job), sonst parallele Schreiber.
            print("  Hinweis: nicht 04:00-05:30 UTC laufen lassen (Server-Jobs aktiv)")
            _db = SessionLocal()
            try:
                _r = _aio.run(_research.daily_store_discovery(_db))
                print(f"  STOREDAILY: stores={_r['neu_verarbeitet']} "
                      f"ideen={_r['ideen']} kandidaten={_r['kandidaten']}")
                for _e in _r["stores"]:
                    print(f"    {_e['store_id']} '{_e.get('name','')[:40]}' -> "
                          f"{'FEHLER: ' + _e.get('fehler','')[:80] if _e.get('kept') is None else str(_e['kept']) + ' Ideen'}")
            finally:
                _db.close()
                _research.release_run()
    elif arg.startswith("syncids:"):
        # MUTIEREND (auf Nutzer-Anweisung 16.08. "fix inkl wieder einstellen der
        # menge"): gezielter Lieferanten-Sync je Listing — gleicher Pfad wie der
        # "Synchronisieren"-Knopf. Heilt falsch-genullte Listings (Menge zurueck).
        import asyncio as _aio
        from app.models import Listing as _L
        from app.services import monitoring_service as _mon
        _db = SessionLocal()

        # EIN Event-Loop fuer alle IDs: die prozessweit gecachten HTTP-Clients sind
        # an ihren Loop gebunden — je ID ein eigenes asyncio.run() liesse ab der
        # zweiten ID "Event loop is closed"-Fehler entstehen.
        async def _sync_alle(_ids):
            for _lid in _ids:
                _l = _db.get(_L, _lid)
                if _l is not None and getattr(_l, "sales_hold", False):
                    print(f"  SYNC {_lid}: uebersprungen (sales_hold – bewusst pausiert)")
                    continue
                try:
                    _r = await _mon.sync_listing(_db, listing_id=_lid)
                    print(f"  SYNC {_lid}: action={_r.get('action')} "
                          f"in_stock={_r.get('in_stock')}")
                except Exception as _exc:  # noqa: BLE001 – Einzel-Fehler zeigen, weiterlaufen
                    print(f"  SYNC {_lid}: FEHLER {str(_exc)[:200]}")

        try:
            _ids = [int(_t) for _t in arg.split(":", 1)[1].split(",") if _t.strip()]
            _aio.run(_sync_alle(_ids))
        finally:
            _db.close()
    elif arg.startswith("srccheck:"):
        # Read-only: "Quelle weg"-Verdacht pruefen — gespeicherte Flags + LIVE-
        # API-Antwort fuer das verknuepfte AliExpress-Produkt (Vorfall 16.08.:
        # Wandaufkleber/Koala-Topf als Quelle-weg markiert, Seite aber abrufbar).
        import asyncio as _aio
        from app.models import Listing as _L, Product as _P
        from app.services import product_research_service as _research
        _lid = int(arg.split(":", 1)[1])
        _db = SessionLocal()
        try:
            _l = _db.get(_L, _lid)
            if _l is None:
                print(f"  listing {_lid}: nicht gefunden")
            else:
                print(f"  listing={_lid} status={_l.listing_status} "
                      f"monitor={getattr(_l, 'monitor_status', '?')} "
                      f"qty={_l.quantity_available}")
                _p = _db.get(_P, _l.product_id) if _l.product_id else None
                if _p is None:
                    print("  KEIN Produkt verknuepft")
                else:
                    print(f"  product={_p.id} ali_id={_p.aliexpress_id} "
                          f"in_stock={getattr(_p, 'in_stock', '?')} "
                          f"price_cny={_p.price_cny}")
                    _ae = _research._real_ae()
                    try:
                        _e = _aio.run(_research._enrich(_ae, _p.aliexpress_id))
                        print(f"  API-LIVE: status={_e.get('status')} "
                              f"preis={_e.get('price_cny')} ships={_e.get('ships_from')} "
                              f"titel='{(_e.get('title') or '')[:50]}'")
                    except Exception as _exc:  # noqa: BLE001
                        print(f"  API-FEHLER: {type(_exc).__name__}: {str(_exc)[:250]}")
        finally:
            _db.close()
    elif arg == "storelist":
        # Stores hinter unseren LOKALEN Ideen sammeln (store_id via Produkt-Detail,
        # falls die Idee sie noch nicht traegt; max. 40 Lookups je Lauf).
        import asyncio as _aio
        from app.models import ProductIdea as _PI
        from app.services import product_research_service as _research
        _db = SessionLocal()
        try:
            _ideas = list(_db.scalars(select(_PI)))
            _local = [i for i in _ideas
                      if _research.api.has_eu_warehouse(i.ships_from)]
            print(f"  LOKALE IDEEN: {len(_local)} von {len(_ideas)}")
            _ae = _research._real_ae()
            _stores: dict = {}
            _lookups = 0
            for _i in _local:
                _sid = getattr(_i, "store_id", None)
                _sname = _i.store_name
                if not _sid and _lookups < 40:
                    try:
                        _e = _aio.run(_research._enrich(_ae, _i.aliexpress_id))
                        _lookups += 1
                        _sid = _e.get("store_id")
                        _sname = _e.get("store_name") or _sname
                        if _sid:            # fuer kuenftige Laeufe merken
                            _i.store_id = _sid
                    except Exception:  # noqa: BLE001
                        continue
                if not _sid:
                    continue
                _v = _stores.setdefault(str(_sid), {"name": _sname, "n": 0})
                _v["n"] += 1
                if _sname and not _v["name"]:
                    _v["name"] = _sname
            _db.commit()
            print(f"  STORES: {len(_stores)} (Detail-Lookups: {_lookups})")
            for _sid, _v in sorted(_stores.items(), key=lambda kv: -kv[1]["n"]):
                print(f"  - Store {_sid} | Produkte bei uns: {_v['n']} | {_v['name'] or '?'}")
        finally:
            _db.close()
    elif arg.startswith("scanids:"):
        # Extern (Browser) geerntete Produkt-IDs durch die normale Discover-
        # Pipeline — Workaround fuer Store-Seiten, die AliExpress an Server-IPs
        # leer ausliefert (Store 1105638009, 14.08.).
        import asyncio as _aio
        from app.services import product_research_service as _research
        _ids = [x.strip() for x in arg.split(":", 1)[1].split(",") if x.strip()]
        print(f"  ID-IMPORT: {len(_ids)} IDs")
        _db = SessionLocal()
        try:
            _r = _aio.run(_research.discover(
                _db, product_ids=_ids, target=15, local_only=False,
                from_trend=True))
            print(f"  ERGEBNIS: {_r}")
        finally:
            _db.close()
    elif arg.startswith("storescan:"):
        # Store-Bestseller-Seite durch die NORMALE Discover-Pipeline — konservativ:
        # kein Lokal-Blankovertrauen (trust_pages=False), Hub bleibt draussen.
        import asyncio as _aio
        from app.integrations import aliexpress_store as _store
        from app.services import product_research_service as _research
        _raw = arg.split(":", 1)[1].strip()
        _sid = _store.extract_store_id(_raw) or _raw
        _url = _store.store_url(_sid)
        print(f"  STORE-SCAN {_sid}: {_url}")
        _db = SessionLocal()
        try:
            _r = _aio.run(_research.discover(
                _db, niches=[_url], target=15, local_only=True,
                use_hub=False, trust_pages=False, from_trend=True))
            print(f"  ERGEBNIS: {_r}")
        finally:
            _db.close()
    elif arg == "elektro":
        # Read-only: alle aktiven Elektro-Kandidaten (Kategorie ODER Titel-Stichwort)
        # mit letztem Verkaufsdatum + Verkaufszahl.
        import re as _re
        from app.models import Listing as _L
        from sqlalchemy import func as _f
        _pat = _re.compile(
            r"HTC|LED|USB|Bluetooth|Digital|Solar|Akku|elektr|wiederaufladbar|"
            r"Laser|Kopfh|Lautsprecher|Ohrh|Smartwatch|Ventilator", _re.IGNORECASE)
        _kats = ("TV, Video & Audio", "Handys & Kommunikation",
                 "Computer, Tablets & Netzwerk", "Foto & Camcorder")
        _db = SessionLocal()
        try:
            _last = {lid: d for lid, d in _db.execute(
                select(Sale.listing_id, _f.max(Sale.sale_date))
                .where(Sale.listing_id.is_not(None)).group_by(Sale.listing_id))}
            _cnt = {lid: n for lid, n in _db.execute(
                select(Sale.listing_id, _f.count())
                .where(Sale.listing_id.is_not(None)).group_by(Sale.listing_id))}
            _aktive = list(_db.scalars(select(_L).where(_L.listing_status == "active")))
            _out = []
            for _l in _aktive:
                _kat = _l.category_name or ""
                if not (_kat.startswith(_kats) or _pat.search(_l.title_seo or "")):
                    continue
                _out.append((str(_last.get(_l.id) or ""), _l.id, _l.ebay_item_id,
                             _cnt.get(_l.id, 0), _kat.split(":")[0][:22],
                             (_l.title_seo or "")[:58]))
            _out.sort(reverse=True)
            print(f"  ELEKTRO-KANDIDATEN (aktiv): {len(_out)}")
            for _ls, _lid, _item, _n, _kat, _title in _out:
                print(f"  - Listing {_lid} | eBay {_item} | letzter Verkauf: "
                      f"{_ls[:10] if _ls else 'NIE'} | Verkäufe: {_n} | "
                      f"{_kat} | {_title}")
        finally:
            _db.close()
    elif arg.startswith("endlist:"):
        # MUTIEREND — NUR auf ausdrueckliche Nutzer-Anweisung (Regel 7; hier:
        # "Beende alle", 14 Elektro-Listings ohne Verkauf, 13.08.). Nutzt exakt
        # den erprobten Dashboard-Pfad end_listing_live (Gruppe/Offer/EndItem).
        import asyncio as _aio
        from app.services import golive_service as _gl
        _ids = [int(x) for x in arg.split(":", 1)[1].split(",") if x.strip()]
        _db = SessionLocal()
        try:
            _ok = 0
            for _lid in _ids:
                try:
                    _r = _aio.run(_gl.end_listing_live(_db, listing_id=_lid))
                    _ok += 1
                    print(f"  BEENDET {_lid}: {_r}")
                except Exception as _exc:  # noqa: BLE001
                    print(f"  FEHLER {_lid}: {type(_exc).__name__}: {str(_exc)[:200]}")
            print(f"  ERGEBNIS: {_ok} von {len(_ids)} beendet")
        finally:
            _db.close()
    elif arg == "neversold":
        # Read-only: aktive Listings OHNE einen einzigen Verkauf (fuer Aufraeum-
        # Entscheidungen des Nutzers — hier wird NICHTS beendet/geloescht).
        from app.models import Listing as _L
        _db = SessionLocal()
        try:
            _sold = {lid for (lid,) in _db.execute(
                select(Sale.listing_id).where(Sale.listing_id.is_not(None)).distinct())}
            _aktive = list(_db.scalars(select(_L).where(_L.listing_status == "active")))
            _out = sorted((l for l in _aktive if l.id not in _sold),
                          key=lambda x: (x.category_name or "?", x.id))
            print(f"  NIE VERKAUFT (aktive Listings): {len(_out)} von {len(_aktive)}")
            for _l in _out[:900]:
                print(f"  - Listing {_l.id} | eBay {_l.ebay_item_id} | "
                      f"Kat: {(_l.category_name or '?')[:45]} | "
                      f"seit {str(_l.listing_start_date or '?')[:10]} | "
                      f"{(_l.title_seo or '')[:60]}")
        finally:
            _db.close()
    elif arg == "notrack":
        # Read-only: Verkaeufe OHNE Sendungsverfolgung (nicht storniert/erstattet),
        # mit Bestell-Status — zeigt, WO das Tracking haengt (nicht verschickt,
        # keine verknuepfte AE-Order, Eigenbestand, Sync-Problem).
        from app.models import OrderAliexpress as _O
        _db = SessionLocal()
        try:
            _sales = list(_db.scalars(select(Sale).order_by(Sale.id.desc()).limit(400)))
            _out = []
            for _s in _sales:
                if _s.status in ("cancelled", "refunded", "delivered"):
                    continue
                _o = _db.scalar(select(_O).where(_O.sale_id == _s.id))
                if _o is not None and _o.tracking_number:
                    continue
                _out.append((
                    _s.id, _s.status,
                    _o.status if _o is not None else "KEINE Bestell-Zeile",
                    (_o.aliexpress_order_id or "—") if _o is not None else "—",
                    str(_o.order_date or "")[:10] if _o is not None else "—",
                    "ja" if _s.ae_paid else "nein"))
            print(f"  OHNE TRACKING (offene Verkaeufe): {len(_out)}")
            for _sid, _st, _ost, _aeid, _od, _paid in _out:
                print(f"  - Sale {_sid} | sale={_st} | order={_ost} | "
                      f"ae_id={_aeid} | bestellt={_od} | bezahlt-markiert={_paid}")
        finally:
            _db.close()
    elif arg == "soldout":
        # Read-only: aktive Listings, die KOMPLETT auf 0 Einheiten stehen
        # (alle Varianten im Spiegel 0 bzw. ohne Varianten Menge 0).
        from app.models import Listing as _L
        _db = SessionLocal()
        try:
            _rows = list(_db.scalars(select(_L).where(_L.listing_status == "active")))
            _out = []
            for _l in _rows:
                _vs = _l.variant_stock or {}
                if _vs:
                    if all(int(v or 0) == 0 for v in _vs.values()):
                        _out.append((_l.id, _l.ebay_item_id,
                                     f"alle {len(_vs)} Varianten 0",
                                     (_l.title_seo or "")[:70]))
                elif int(_l.quantity_available or 0) == 0:
                    _out.append((_l.id, _l.ebay_item_id, "Menge 0",
                                 (_l.title_seo or "")[:70]))
            print(f"  KOMPLETT AUSVERKAUFT (aktive Listings, 0 Einheiten): {len(_out)} "
                  f"von {len(_rows)} aktiven")
            for _lid, _item, _why, _title in _out:
                print(f"  - Listing {_lid} | eBay {_item} | {_why} | {_title}")
        finally:
            _db.close()
    elif arg == "trends":
        import asyncio as _aio
        from app.config import get_settings as _gs
        from app.services import product_research_service as _research
        _db = SessionLocal()
        try:
            _s = _gs()
            _r = _aio.run(_research.discover_trends(
                _db, target=_s.trend_research_target,
                max_delivery=_s.trend_research_max_delivery, local_only=True,
                fallback_china=True))
            print(f"  TREND-TESTLAUF: keywords={len(_r.get('trends', []))} "
                  f"kept={_r.get('kept')} scanned={_r.get('scanned')} "
                  f"local_uebersprungen={_r.get('local_skipped')} "
                  f"china_kept={_r.get('china_kept')} note={_r.get('note')}")
        finally:
            _db.close()
    else:
        main(int(arg or 25))
