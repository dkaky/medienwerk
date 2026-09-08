"""Bereich 4: Listing-Optimierung (Spec Kap. 5.4).

Woechentlicher Lauf: aktive Listings -> Performance von eBay -> 0-Klick-
Kandidaten filtern -> Konkurrenzanalyse + neuer Titel (LLM) -> Update zu eBay
-> Eskalationsstufen 1 (Titel/Kategorie) -> 2 (Bild) -> 3 (Delisting).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations import get_llm_client
from app.models import Listing
from app.retry import retry_async
from app.services.common import task_log

logger = logging.getLogger("app.services.optimization")


def _real_ebay():
    """ECHTER eBay-Client, unabhaengig vom MOCK_EBAY-Flag. Auf dem VPS ist
    get_ebay_client() gemockt (Reads), aber Analytics (0-Klick-Erkennung),
    Marktrecherche (Konkurrenz-Titel) und der Titel-Push muessen ECHT sein –
    wie bei import/golive/fulfillment (die ebenfalls _real_ebay nutzen)."""
    from app.integrations.ebay import RealEbayClient
    return RealEbayClient(get_settings())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Lauf-Lock: die manuelle "Analysieren"-Aktion laeuft im Hintergrund (392 Listings,
# eBay-Analytics + Marktrecherche + LLM = Minuten). Ein zweiter Klick darf keinen
# Parallel-Lauf starten.
_opt_lock = threading.Lock()
_opt_running = False
# Ergebnis des letzten Klick-Refreshs (fuer die UI-Rueckmeldung: hat eBay-Analytics
# Daten geliefert oder ist der Abruf fehlgeschlagen?).
_last_refresh: dict = {}


def try_acquire_opt() -> bool:
    global _opt_running
    with _opt_lock:
        if _opt_running:
            return False
        _opt_running = True
        return True


def release_opt() -> None:
    global _opt_running
    with _opt_lock:
        _opt_running = False


def is_opt_running() -> bool:
    return _opt_running


def set_last_refresh(result: dict) -> None:
    global _last_refresh
    _last_refresh = result


def last_refresh() -> dict:
    return _last_refresh


def list_performance(db: Session, *, status: str | None = "active",
                     clicks_min: int | None = None, clicks_max: int | None = None) -> dict:
    """GET .../listings/performance – aktuelle Kennzahlen + Aggregat-Stats."""
    stmt = select(Listing)
    if status:
        stmt = stmt.where(Listing.listing_status == status)
    if clicks_min is not None:
        stmt = stmt.where(Listing.clicks_week >= clicks_min)
    if clicks_max is not None:
        stmt = stmt.where(Listing.clicks_week <= clicks_max)
    rows = db.scalars(stmt).all()

    items = []
    for lst in rows:
        impressions = lst.impressions_week or 0
        clicks = lst.clicks_week or 0
        items.append({
            "listing_id": lst.id,
            "title": lst.title_seo,
            "impressions": impressions,
            "clicks": clicks,
            "ctr": round(clicks / impressions, 4) if impressions else 0.0,
            "optimization_status": lst.optimization_status,
            "last_changed": lst.last_optimization_date,
            "category": lst.category_id,
        })

    all_active = db.scalars(
        select(Listing).where(Listing.listing_status == "active")
    ).all()
    stats = {
        "total": db.scalar(select(func.count()).select_from(Listing)) or 0,
        "active": len(all_active),
        "zero_clicks": sum(1 for l in all_active if (l.clicks_week or 0) == 0),
        "in_escalation": sum(1 for l in all_active if l.optimization_status == "in_escalation"),
    }
    return {"listings": items, "stats": stats}


async def apply_stage(db: Session, *, listing_id: int, stage: int,
                      new_title: str | None = None, new_category: str | None = None,
                      new_image_index: int | None = None, reason: str | None = None) -> dict:
    """POST .../optimize/{listing_id}/stage/{stage} – eine Optimierungsstufe anwenden."""
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ValueError("Listing nicht gefunden")

    ebay = _real_ebay()
    changes: list[str] = []

    if new_title:
        listing.title_seo = new_title
        changes.append("title")
    if new_category:
        listing.category_id = new_category
        changes.append("category")
    if new_image_index is not None:
        changes.append("image")

    with task_log(db, task_type="optimize", reference_id=listing_id) as tl:
        await retry_async(
            lambda: ebay.update_inventory(
                listing.ebay_item_id or f"item_{listing_id}",
                title=listing.title_seo, category_id=listing.category_id,
            ),
            label="ebay_update",
        )
        listing.optimization_stage = stage
        listing.optimization_status = "in_escalation" if stage >= 1 else "needs_review"
        listing.last_optimization_date = _now()
        # KEIN Auto-Beenden mehr (Nutzerregel: Listings nie automatisch beenden).
        # Frueher: stage>=3 -> listing_status="ended". Stufe 3 markiert nur noch.
        tl.result_data = {"stage": stage, "changes": changes, "reason": reason}
        db.commit()

    next_review = _now() + timedelta(days=get_settings().opt_zero_click_days)
    return {
        "listing_id": listing_id,
        "stage": stage,
        "changes_applied": changes,
        "next_review": next_review,
    }


# Muster, die einen "Titel" als KI-Geschwaetz statt echten Titel entlarven. Die KI
# darf NIE einen solchen String als eBay-Titel bekommen (Vorfall 07/2026: 115 Titel
# mit "Ich bin bereit, einen Titel zu erstellen ..." ueberschrieben).
_CHAT_TITLE_MARKERS = (
    "ich ", "ich,", "gerne", "hier ist", "hier sind", "vorschlag", "natürlich",
    "natuerlich", "als ki", "als assistent", "leider", "entschuldigung",
    "ich kann", "ich sehe", "ich bin", "ich würde", "ich wuerde", "i can",
    "i'm", "i am", "here is", "here's", "sure", "certainly", "as an ai",
    "konkurrenz-titel", "wettbewerber", "auflisten", "keine daten",
)


def _fix_faux_leather(title: str) -> str:
    """Unsere Produkte sind KUNSTLEDER – nie echtes 'Leder' im Titel behaupten (Nutzerregel).
    'Echt(es) Leder' / 'Echtleder' / (Wortanfang) 'Leder…' -> 'Kunstleder…'.
    'Kunstleder' bleibt unangetastet (kein Wortanfang vor 'leder')."""
    import re
    if not title:
        return title
    t = re.sub(r"\bEcht(?:es|em|er|e)?[\s-]*Leder", "Kunstleder", title, flags=re.IGNORECASE)
    t = re.sub(r"\bEchtleder\b", "Kunstleder", t, flags=re.IGNORECASE)
    t = re.sub(r"\bLeder", "Kunstleder", t, flags=re.IGNORECASE)
    return t


def _clean_suggested_title(raw: str | None, current: str | None) -> str | None:
    """KI-Titelvorschlag saeubern & validieren. Gibt einen sauberen eBay-Titel
    (<=80 Zeichen) zurueck ODER None, wenn der Vorschlag unbrauchbar ist.

    Abgelehnt wird: leer, Chat-/Erklaer-Text, mehrzeilig, zu lang/zu kurz, identisch
    zum aktuellen Titel. Lieber KEIN Vorschlag als ein kaputter Titel.
    """
    if not raw:
        return None
    t = raw.strip().strip('"').strip("'").strip()
    # Nur die erste Zeile ist ein Titel; mehrzeilige Antworten sind Erklaerungen.
    if "\n" in t:
        return None
    # Untergrenze 20 (kuerzer = eher verdaechtige Chat-/Fehlerausgabe als echter Titel).
    if not (20 <= len(t) <= 80):
        return None
    low = t.lower()
    if any(m in low for m in _CHAT_TITLE_MARKERS):
        return None
    # Satzzeichen-Endungen (".", ":", "?") deuten auf Fliesstext, nicht auf Titel.
    if t.endswith((".", ":", "?", "…")):
        return None
    # Kunstleder-Regel (nach der Laengen-/Junk-Pruefung): nie echtes 'Leder' behaupten;
    # bei >80 auf Wortgrenze kuerzen. MUSS vor dem Identisch-Check laufen, sonst rutscht ein
    # Vorschlag durch, der erst NACH der Normalisierung identisch zum aktuellen Titel ist.
    t = _fix_faux_leather(t)
    # Keine ERFUNDENEN Mengen-Behauptungen im Vorschlag (Vorfall Zoro 19.08.:
    # "3er-Set" fuer einen Ein-Paar-Artikel) — Zahl+Zaehleinheit nur, wenn der
    # aktuelle Titel sie schon traegt.
    from app.integrations.llm import strip_invented_counts
    t, _mengen_weg = strip_invented_counts(current or "", t)
    if len(t) > 80:
        t = t[:80].rsplit(" ", 1)[0].strip()
    if current and t.strip() == (current or "").strip():
        return None
    return t


def _search_query(title: str, words: int = 5) -> str:
    """Kern-Keywords fuer die Konkurrenz-Suche: die ersten N Woerter des Titels.
    Der volle 80-Zeichen-Titel ist zu spezifisch und findet nur das eigene Listing."""
    return " ".join((title or "").split()[:words]).strip()


async def _market_research(ebay, listing: Listing, *, limit: int = 10) -> list[str]:
    """Echte Konkurrenz-Titel zu einem Listing (Browse API), ohne das eigene Listing."""
    q = _search_query(listing.title_seo or "")
    if not q:
        return []
    own = (listing.title_seo or "").strip().lower()
    titles = await ebay.search_competitor_titles(q, limit=limit + 3)
    return [t for t in titles if t.strip().lower() != own][:limit]


def _bestseller_titles(db: Session, listing: Listing, *, limit: int = 8) -> list[str]:
    """Titel EIGENER, gut verkaufter aehnlicher Listings als Vorbild (interne „Winning
    Products"). Gleiche Top-Level-Kategorie bevorzugt, nach Verkaeufen sortiert; das eigene
    Listing raus. So orientiert sich der Vorschlag an dem, was im SHOP nachweislich laeuft –
    statt an willkuerlichen Formulierungen (Nutzerwunsch 09.07.)."""
    top = (listing.category_name or "").split(":")[0].strip()
    rows = db.scalars(
        select(Listing).where(
            Listing.id != listing.id,
            Listing.listing_status == "active",
            Listing.sales_total.isnot(None), Listing.sales_total > 0,
            Listing.title_seo.isnot(None),
        ).order_by(Listing.sales_total.desc()).limit(60)).all()
    # Gleiche Top-Level-Kategorie bevorzugen; sonst allgemein die shop-weiten Bestseller.
    same_cat = [l for l in rows if top and (l.category_name or "").split(":")[0].strip() == top]
    own = (listing.title_seo or "").strip().lower()
    seen, out = set(), []
    for l in (same_cat or rows):
        t = (l.title_seo or "").strip()
        key = t.lower()
        if t and key != own and key not in seen:
            seen.add(key)
            out.append(t)
        if len(out) >= limit:
            break
    return out


async def _suggest_title_for(llm, listing: Listing,
                             competitor_titles: list[str] | None = None,
                             internal_titles: list[str] | None = None) -> str | None:
    """Einen validierten, marktrecherche-gestuetzten Titelvorschlag holen (oder None).

    Signal: erfolgreiche KONKURRENZ-Titel (eBay Best-Match) + eigene BESTSELLER-Titel –
    der Vorschlag orientiert sich an nachweislich verkaufenden Titeln, nicht an Willkuer.
    """
    try:
        raw = await llm.suggest_title(current_title=listing.title_seo or "",
                                      competitor_titles=competitor_titles or [],
                                      internal_titles=internal_titles or [])
    except Exception:  # noqa: BLE001 – ein KI-Fehler darf den Lauf nicht stoppen
        return None
    return _clean_suggested_title(raw, listing.title_seo)


async def run_weekly_optimization(db: Session, *, dry_run: bool = False) -> dict:
    """PROPOSE-ONLY (ab 07/2026): schlaegt fuer 0-Klick-Listings einen neuen Titel VOR
    und markiert sie als 'needs_review'. Es wird NICHTS automatisch auf eBay geaendert
    und NIE ein Listing beendet – die Freigabe passiert manuell im Optimierungs-Tab.
    """
    settings = get_settings()
    ebay = _real_ebay()
    llm = get_llm_client()
    threshold = _now() - timedelta(days=settings.opt_zero_click_days)

    listings = db.scalars(
        select(Listing)
        .where(Listing.listing_status == "active")
        .order_by(Listing.last_optimization_date.is_(None).desc(),
                  Listing.last_optimization_date.asc())
    ).all()

    # Step 2: Performance via EINEM Sammel-Traffic-Report (quota-schonend, s.
    # refresh_click_data). Danach spiegeln die 'listings' die frischen Klicks (gleiche
    # Session). Ohne echte Credentials (Dev/Test) bleibt alles unveraendert.
    await refresh_click_data(db)

    # Step 3: Kandidaten = 0 Klicks, lange nicht optimiert, ohne offenen Vorschlag
    candidates = [
        l for l in listings
        if (l.clicks_week or 0) == 0
        and not l.optimization_suggestion
        and (l.last_optimization_date is None or l.last_optimization_date < threshold)
    ]

    suggestions_created = 0
    if not dry_run:
        for lst in candidates:
            # MARKTRECHERCHE: echte Konkurrenz-Titel (eBay Best-Match) als Signal,
            # welche Formulierungen sich verkaufen -> die KI orientiert sich daran.
            # Kern-Keywords statt Volltitel suchen (der 80-Zeichen-Titel findet nur
            # das eigene Listing); eigenes Listing aus den Treffern filtern.
            competitors = await _market_research(ebay, lst)
            suggestion = await _suggest_title_for(llm, lst, competitors, _bestseller_titles(db, lst))
            lst.last_optimization_date = _now()
            if suggestion:
                lst.optimization_suggestion = suggestion
                lst.optimization_status = "needs_review"
                suggestions_created += 1
        db.commit()

    return {
        "task_id": f"opt_weekly_{_now().strftime('%Y%m%d')}",
        "listings_analyzed": len(listings),
        "candidates": len(candidates),
        "suggestions_created": suggestions_created,
        "status": "completed" if not dry_run else "dry_run",
    }


def list_suggestions(db: Session) -> dict:
    """Offene KI-Titelvorschlaege fuers Optimierungs-Tab (nichts ist angewandt)."""
    rows = db.scalars(
        select(Listing).where(Listing.optimization_suggestion.isnot(None),
                              Listing.listing_status == "active")
        .order_by(Listing.last_optimization_date.desc())
    ).all()
    items = [{
        "listing_id": l.id,
        "ebay_item_id": l.ebay_item_id,
        "current_title": l.title_seo,
        "suggested_title": l.optimization_suggestion,
        "image": l.image_url,
        "price_eur": float(l.price_eur) if l.price_eur is not None else None,
    } for l in rows]
    return {"count": len(items), "suggestions": items}


def list_candidates(db: Session, *, max_clicks: int = 0,
                    clicks_exact: int | None = None) -> dict:
    """AKTIVE Optimierungs-Kandidaten (aus den zuletzt gesyncten Klick-Kennzahlen).

    * ``clicks_exact`` gesetzt -> NUR Listings mit GENAU dieser Klickzahl (freier
      Filter im UI, z.B. exakt 0/1/2). Sonst: hoechstens ``max_clicks`` Klicks.
    * 14-TAGE-AUSBLENDEN: kuerzlich optimierte Listings (optimized_at < opt_hide_days)
      werden ausgeblendet – sie bekommen Zeit zu wirken und kommen danach wieder,
      falls sie die Klick-Schwelle noch erfuellen.
    * SORTIERUNG: aelteste Listings zuerst (am laengsten online = wichtigster Hebel;
      neue brauchen erst Zeit). Bei gleichem Alter mehr Impressions zuerst.
    """
    from datetime import timedelta
    from app.models import Sale
    settings = get_settings()
    clicks_expr = func.coalesce(Listing.clicks_week, 0)
    stmt = select(Listing).where(Listing.listing_status == "active")
    if clicks_exact is not None:
        stmt = stmt.where(clicks_expr == int(clicks_exact))
        mc = None
    else:
        mc = max(0, int(max_clicks or 0))
        stmt = stmt.where(clicks_expr <= mc)
    # 14-Tage-Ausblenden kuerzlich uebernommener Optimierungen.
    hide_cutoff = _now() - timedelta(days=max(0, settings.opt_hide_days))
    stmt = stmt.where((Listing.optimized_at.is_(None)) | (Listing.optimized_at < hide_cutoff))
    # Aelteste zuerst (echtes eBay-Einstelldatum, sonst DB-Anlage), dann meiste Impressions.
    age_key = func.coalesce(Listing.listing_start_date, Listing.created_at)
    rows = db.scalars(stmt.order_by(age_key.asc(), Listing.impressions_week.desc().nullslast(),
                                    Listing.id.asc())).all()

    # Letzter Verkauf je Listing (nur gueltige Sales; None -> UI zeigt 0).
    _void = ("refunded", "cancelled", "canceled", "storniert")
    last_sale = dict(db.execute(
        select(Sale.listing_id, func.max(func.coalesce(Sale.sale_date, Sale.created_at)))
        .where(Sale.listing_id.isnot(None), Sale.status.notin_(_void))
        .group_by(Sale.listing_id)).all())

    items = [{
        "listing_id": l.id,
        "title": l.title_seo,
        "image": l.image_url,
        "clicks": l.clicks_week or 0,
        "impressions": l.impressions_week or 0,
        "suggestion": l.optimization_suggestion,   # bereits ein Vorschlag vorhanden?
        "ebay_item_id": l.ebay_item_id,
        "sales_total": l.sales_total,
        # "seit wann online" (echtes eBay-Datum, sonst DB-Anlage als Fallback).
        "online_since": (l.listing_start_date or l.created_at),
        "online_since_real": l.listing_start_date is not None,
        # letzter Verkauf; None -> UI zeigt "0" (noch nie verkauft).
        "last_sale_date": last_sale.get(l.id),
    } for l in rows]
    return {"count": len(items), "candidates": items,
            "max_clicks": mc, "clicks_exact": clicks_exact,
            "window_days": settings.opt_zero_click_days,
            "hide_days": settings.opt_hide_days,
            "last_refresh": last_refresh() or None}


def _aware(dt):
    """Naives Datum als UTC interpretieren (SQLite liefert naiv, Postgres aware) –
    damit Vergleiche nicht an tz-aware/naive scheitern."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def list_optimization_buckets(db: Session) -> dict:
    """Kandidaten in 3 handlungsleitende Funnel-Buckets – je mit dem PASSENDEN Hebel,
    statt einer flachen 0-Klick-Liste (die den falschen Hebel zog: 88% der nie
    verkauften Listings bekommen Klicks, konvertieren aber nicht -> Titel aendern
    hilft dort nicht, Preis/Angebot schon).

      * price      – nie verkauft, ABER Klicks vorhanden (Conversion-Killer):
                     Sichtbarkeit+CTR stimmen, es hakt am Angebot -> Preis/Fotos/
                     Beschreibung. Sortiert nach meisten Klicks (verschwendete
                     Aufmerksamkeit zuerst; schlaegt reines Alter).
      * title      – nie verkauft, Impressionen ABER 0 Klicks (CTR-Problem): wird
                     gezeigt, reizt nicht -> Titel/Hauptbild. Meiste Impressionen zuerst.
      * reactivate – schon >=1x verkauft, aber seit >opt_zero_click_days kein Verkauf
                     (kalt gewordener Selbstlaeufer) -> Preis/Saison/Relist. Laengste
                     Durststrecke zuerst.

    Kuerzlich uebernommene Optimierungen (optimized_at < opt_hide_days) sind in
    price/title ausgeblendet (Wirkzeit). Listings ohne jede Sichtbarkeit (0 Impr,
    0 Klicks, nie verkauft) haben aktuell keinen eigenen Bucket – sie tauchen auf,
    sobald eBay Traffic meldet.
    """
    from datetime import timedelta
    from app.models import Product, Sale
    from app.services import pricing
    from app.services.listing_match_service import source_is_confirmed, effective_ebay_price
    settings = get_settings()
    now = _now()
    hide_cutoff = now - timedelta(days=max(0, settings.opt_hide_days))
    cold_cutoff = now - timedelta(days=max(1, settings.opt_zero_click_days))
    cleanup_cutoff = now - timedelta(days=max(1, settings.cleanup_min_age_days))
    min_age = timedelta(days=max(0, settings.opt_min_age_days))
    max_impr = max(0, settings.cleanup_max_impressions)

    listings = db.scalars(
        select(Listing).where(Listing.listing_status == "active")).all()
    products = {p.id: p for p in db.scalars(select(Product)).all()}

    def _unverified(l: Listing) -> bool:
        """Nur VERIFIZIERTE Quellen optimieren. Ausgeschlossen: unbestaetigte Auto-Bild-Matches
        UND Listings OHNE Quelle (product_id None). Letzteres tritt auf, wenn der Nutzer eine
        FALSCHE Quelle entfernt hat – ohne Quelle ist das Produkt nicht verifiziert und die
        EK-/Preis-Empfehlung unzuverlaessig, also raus aus der Optimier-Liste (Nutzerregel 09.07.)."""
        p = products.get(l.product_id) if l.product_id else None
        if p is None:
            return True
        return not source_is_confirmed(p)

    _void = ("refunded", "cancelled", "canceled", "storniert")
    last_sale = dict(db.execute(
        select(Sale.listing_id, func.max(func.coalesce(Sale.sale_date, Sale.created_at)))
        .where(Sale.listing_id.isnot(None), Sale.status.notin_(_void))
        .group_by(Sale.listing_id)).all())

    def _online_since(l: Listing):
        return _aware(l.listing_start_date or l.created_at)

    def _item(l: Listing) -> dict:
        impr = l.impressions_week or 0
        clk = l.clicks_week or 0
        since = _online_since(l)
        # Aktueller Netto-Gewinn + Marge (mit echter Anzeigenrate des Listings – eine
        # Rechen-Wahrheit wie im Cockpit), damit der Optimieren-Reiter Gewinn/Marge zeigt.
        _ek = float(l.cost_eur) if l.cost_eur is not None else None
        # EINHEITLICH: gegen den ECHTEN eBay-Preis rechnen (konservativ, falls SKU-Keys nicht
        # matchen), sonst intern als Fallback vor dem ersten Abgleich – gleiche Wahrheit wie das Cockpit.
        _live = effective_ebay_price(l)
        _cur = _live if _live is not None else (float(l.price_eur) if l.price_eur is not None else None)
        _price_is_live = _live is not None
        if _ek is not None and _cur is not None and _cur > 0:
            _feep = pricing.effective_fee_pct_for_listing(l, settings=settings)
            _prof = round(_cur - _cur * _feep - pricing.ebay_fixed_fee(settings) - _ek, 2)
            _marg = round(_prof / _cur, 4)
        else:
            _prof = _marg = None
        return {
            "listing_id": l.id,
            "title": l.title_seo,
            "image": l.image_url,
            "clicks": clk,
            "impressions": impr,
            "ctr": round(clk / impr, 4) if impr else 0.0,
            "suggestion": l.optimization_suggestion,
            "ebay_item_id": l.ebay_item_id,
            "sales_total": l.sales_total or 0,
            "online_since": (l.listing_start_date or l.created_at),
            "online_since_real": l.listing_start_date is not None,
            "age_days": (now - since).days if since else None,
            "price_eur": _cur,                # echter eBay-Preis (Fallback: intern)
            "price_is_live": _price_is_live,
            "profit_eur": _prof, "margin_pct": _marg,
            "last_sale_date": last_sale.get(l.id),
        }

    def _price_cut(l: Listing):
        """Ladenhueter-Senkung: Zielpreis auf upload_margin_pct (25 %) – NUR wenn danach noch
        im Plus UND spuerbar guenstiger als jetzt. Sonst None (kein Vorschlag)."""
        ek = float(l.cost_eur) if l.cost_eur is not None else None
        cur = float(l.price_eur) if l.price_eur is not None else None
        if ek is None or cur is None or cur <= 0:
            return None
        fl = pricing.price_floor(ek, min_margin_pct=settings.upload_margin_pct,
                                 category_name=l.category_name,
                                 ad_rate_pct=getattr(l, "ad_rate_pct", None), settings=settings)
        if fl is None:
            return None
        target = pricing.round_up_to_cents(fl, settings.price_cents or 0.95)
        if target >= cur - 0.05:      # schon am/unter Zielmarge -> kein Spielraum
            return None
        new_profit = pricing.profit_at_price(target, ek, category_name=l.category_name, settings=settings)
        if new_profit is None or new_profit <= 0:
            return None                # nur senken, wenn danach noch Gewinn bleibt
        cur_profit = pricing.profit_at_price(cur, ek, category_name=l.category_name, settings=settings)
        return {
            "suggested_price_eur": target,
            "current_profit_eur": cur_profit,
            "suggested_profit_eur": new_profit,
            "current_margin": round(cur_profit / cur, 4) if cur_profit is not None else None,
            "suggested_margin": round(new_profit / target, 4) if target else None,
        }

    price, title, reactivate, cleanup = [], [], [], []
    for l in listings:
        # „So lassen" (verworfen): der Artikel soll unveraendert weiterlaufen -> aus ALLEN
        # Handlungs-Buckets raus (die Nachverfolgung bleibt separat, falls schon optimiert).
        if l.opt_dismissed_at is not None:
            continue
        sold_ever = (l.sales_total or 0) > 0 or l.id in last_sale
        if sold_ever:
            if _unverified(l):         # unbestaetigte ODER fehlende Quelle nicht optimieren
                continue
            ls = _aware(last_sale.get(l.id))
            if ls is None or ls < cold_cutoff:   # kalt gewordener Selbstlaeufer
                reactivate.append(l)
            continue
        # nie verkauft: kuerzlich optimierte ausblenden (Wirkzeit)
        if l.optimized_at is not None and _aware(l.optimized_at) >= hide_cutoff:
            continue
        clk = l.clicks_week or 0
        impr = l.impressions_week or 0
        since = _online_since(l)
        keepable = (l.cleanup_dismissed_at is None and not l.publish_queued
                    and l.optimization_status != "in_escalation")
        # AUFRAEUMEN (Schwelle 02.08.2026 von 5 auf 100 Impr. korrigiert – 5 war unerreichbar,
        # der Reiter blieb dauerhaft leer): 0 Klicks + <= max_impr Impressionen +
        # nie verkauft = "keine Sichtbarkeit". Dann raus, wenn ENTWEDER alt genug (>=60 Tage)
        # ODER schon optimiert und trotz 14 Tagen Wirkzeit keine Besserung (optimized_at
        # gesetzt, Hide-Fenster oben bereits durch).
        dead = (clk == 0 and impr <= max_impr)
        aged = since is not None and since < cleanup_cutoff
        opt_failed = l.optimized_at is not None      # wurde optimiert, 14d rum, immer noch tot
        if dead and keepable and (aged or opt_failed):
            cleanup.append(l)
            continue
        # Ab hier: noch optimierbar -> Mindestalter (nicht zu frueh) + Quelle muss bestaetigt sein.
        # WICHTIG: NUR das ECHTE eBay-Online-Datum (listing_start_date) zaehlt fuers Alter –
        # NICHT das DB-/Entwurfsdatum (created_at). Sonst rutscht ein frisch gelistetes Produkt,
        # das vorher lange Entwurf war, mit altem created_at durch. Fehlt das echte Datum
        # (noch nicht von eBay geholt), gilt das Alter als UNBEKANNT -> nicht optimieren
        # („⟳ Von eBay importieren" holt es; frische Publishes stempeln es jetzt selbst).
        online = _aware(l.listing_start_date)
        too_new = online is None or (now - online) < min_age
        if too_new or _unverified(l):
            continue
        if clk > 0:
            price.append(l)          # Klicks, aber kein Verkauf -> Angebot/Preis
        elif impr > 0:
            title.append(l)          # gezeigt, aber kein Klick -> Titel/Bild

    def _days_cold(l: Listing) -> int:
        ls = _aware(last_sale.get(l.id))
        return (now - ls).days if ls else 10 ** 6

    # Sortierung: AELTESTE zuerst (Nutzerwunsch – neue Artikel nicht bevorzugen); bei
    # gleichem Alter der groesste Hebel (meiste Klicks/Impressionen) zuerst.
    price.sort(key=lambda l: (_online_since(l) or now, -(l.clicks_week or 0)))
    title.sort(key=lambda l: (_online_since(l) or now, -(l.impressions_week or 0)))
    reactivate.sort(key=_days_cold, reverse=True)   # laengste Durststrecke zuerst
    cleanup.sort(key=lambda l: _online_since(l) or now)   # aelteste (laengste Nulldiät) zuerst

    # NACHVERFOLGUNG: alle optimierten Listings (mit Vorher-Snapshot) – Entwicklung der
    # Klicks/Impressionen/Verkaeufe seit der Optimierung. Neueste Optimierung zuerst.
    def _track_item(l: Listing) -> dict:
        snap = l.opt_snapshot if isinstance(l.opt_snapshot, dict) else {}
        cur = {"clicks": l.clicks_week or 0, "impressions": l.impressions_week or 0,
               "sales": l.sales_total or 0}
        before = {"clicks": int(snap.get("clicks") or 0),
                  "impressions": int(snap.get("impressions") or 0),
                  "sales": int(snap.get("sales") or 0)}
        oa = _aware(l.optimized_at)
        return {
            "listing_id": l.id, "title": l.title_seo, "image": l.image_url,
            "ebay_item_id": l.ebay_item_id,
            "optimized_at": l.optimized_at,
            "days_since": (now - oa).days if oa else None,
            "before": before, "after": cur,
            "delta": {k: cur[k] - before[k] for k in cur},
            "price_before_eur": snap.get("price_eur"),
            "price_eur": float(l.price_eur) if l.price_eur is not None else None,
        }
    tracked = [l for l in listings if isinstance(l.opt_snapshot, dict) and l.opt_snapshot]
    tracked.sort(key=lambda l: _aware(l.optimized_at) or now, reverse=True)

    return {
        "buckets": {
            "price": [_item(l) for l in price],
            "title": [_item(l) for l in title],
            "reactivate": [{**_item(l), "price_cut": _price_cut(l)} for l in reactivate],
            "cleanup": [_item(l) for l in cleanup],
            "tracked": [_track_item(l) for l in tracked],
        },
        "counts": {"price": len(price), "title": len(title),
                   "reactivate": len(reactivate), "cleanup": len(cleanup),
                   "tracked": len(tracked)},
        "hide_days": settings.opt_hide_days,
        "cold_days": settings.opt_zero_click_days,
        "cleanup_min_age_days": settings.cleanup_min_age_days,
        "min_age_days": settings.opt_min_age_days,
        "last_refresh": last_refresh() or None,
    }


async def suggest_one(db: Session, *, listing_id: int) -> dict:
    """ON-DEMAND: fuer EIN Listing Marktrecherche + KI-Titelvorschlag erzeugen und
    speichern (propose-only). Schnell (1 Browse + 1 LLM-Call)."""
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ValueError("Listing nicht gefunden")
    ebay = _real_ebay()
    llm = get_llm_client()
    competitors = await _market_research(ebay, listing)
    suggestion = await _suggest_title_for(llm, listing, competitors, _bestseller_titles(db, listing))
    if not suggestion:
        return {"listing_id": listing_id, "suggestion": None,
                "message": "Kein sinnvoller Vorschlag (KI unsicher) – Titel bleibt."}
    listing.optimization_suggestion = suggestion
    listing.optimization_status = "needs_review"
    listing.last_optimization_date = _now()
    db.commit()
    return {"listing_id": listing_id, "suggestion": suggestion,
            "competitors": competitors[:5]}


def _price_stats(prices: list[float]) -> dict | None:
    """Min/Median/Max/Anzahl einer Preisliste (None bei leer)."""
    vals = sorted(p for p in prices if p and p > 0)
    if not vals:
        return None
    n = len(vals)
    median = vals[n // 2] if n % 2 else round((vals[n // 2 - 1] + vals[n // 2]) / 2, 2)
    return {"min": round(vals[0], 2), "median": round(median, 2),
            "max": round(vals[-1], 2), "count": n}


async def _fetch_competitor_offers(ebay, listing) -> list[dict]:
    """Konkurrenz-Angebote (mit Preisen) via Browse; eigenes Listing rausfiltern. Best-effort."""
    q = _search_query(listing.title_seo or "")
    if not q:
        return []
    own = (listing.title_seo or "").strip().lower()
    try:
        raw = await ebay.search_competitor_offers(q, limit=25)
        return [o for o in raw if (o.get("title") or "").strip().lower() != own]
    except Exception as exc:  # noqa: BLE001 – best-effort
        logger.warning("competitor offers fetch failed",
                       extra={"listing_id": listing.id, "error": str(exc)[:150]})
        return []


async def _fetch_listing_images(listing, product) -> tuple[list, str]:
    """Bilder fuer die Titelbild-Auswahl/-Bewertung. LIVE-Listing: die ECHTEN eBay-Bilder
    (die evtl. FALSCHEN AliExpress-Quell-Bilder werden NICHT gezeigt); Entwurf/Mock oder
    wenn eBay gerade nicht liefert: Quell-Bilder. Rueckgabe: (imgs, source) mit
    source in {"ebay", "quelle"}."""
    from app.services import golive_service as gl
    imgs: list = []
    source = "quelle"
    if listing.ebay_item_id:
        try:
            imgs = await gl.ebay_listing_images(_real_ebay(), listing)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ebay images fetch failed",
                           extra={"listing_id": listing.id, "error": str(exc)[:120]})
        if imgs:
            source = "ebay"
    if not imgs:
        imgs = gl.image_candidates(product)
    return imgs, source


async def _compute_market(db: Session, listing, offers: list[dict]) -> dict:
    """Marktanalyse aus VORGEHOLTEN Konkurrenz-Angeboten (kein weiterer Netz-Call):
    Preisspanne + empfohlener Preis je Variante (EK+Zielmarge, x,95, KI-verfeinert) +
    Push-Empfehlungen. Enthaelt fee_pct/fixed_fee fuer die Live-Margen-Anzeige im UI.
    """
    from app.models import Product
    from app.services import golive_service as gl, pricing
    settings = get_settings()
    product = db.get(Product, listing.product_id) if listing.product_id else None
    price_range = _price_stats([o["price_eur"] for o in offers if o.get("price_eur")])

    rows = gl.variant_price_rows(listing, product, settings)
    variants = []
    for r in rows:
        rec = None
        if r.get("ek_eur"):
            rec = gl._price_for_mode(r["ek_eur"], mode="margin",
                                     value=settings.target_margin_pct * 100, settings=settings,
                                     category_name=listing.category_name)
        variants.append({"key": r["sku"], "name": r.get("name") or r["sku"],
                         "ek_eur": r.get("ek_eur"), "current_price_eur": r.get("current_price_eur"),
                         "calc_recommended_eur": rec,
                         # Verlustfreie Untergrenze fuer Senkungen (>= 20% Marge) – KATEGORIE-GENAU
                         # (sonst pauschale 22% statt echter Provision+Anzeigenrate -> falscher Boden).
                         "price_floor_eur": pricing.price_floor(
                             r.get("ek_eur"), category_name=listing.category_name,
                             ad_rate_pct=getattr(listing, "ad_rate_pct", None), settings=settings)})

    llm = get_llm_client()
    ki = {"price_recommendations": [], "push_recommendations": [], "market_note": ""}
    try:
        ki = await llm.analyze_market(
            product_title=listing.title_seo or "",
            variants=[{"key": v["key"], "name": v["name"], "ek_eur": v["ek_eur"],
                       "current_price_eur": v["current_price_eur"],
                       "price_floor_eur": v["price_floor_eur"]} for v in variants],
            competitor_prices=[{"title": o.get("title"), "price_eur": o.get("price_eur")}
                               for o in offers if o.get("price_eur")],
            current_price_eur=float(listing.price_eur) if listing.price_eur else None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("analyze_market call failed", extra={"error": str(exc)[:150]})
    ki_by_key = {str(r.get("variant_key")): r for r in (ki.get("price_recommendations") or [])}

    cents = settings.price_cents or 0.95
    for v in variants:
        ki_rec = ki_by_key.get(v["key"])
        rec = None
        if ki_rec and ki_rec.get("recommended_price_eur"):
            # AUFrunden auf x,95 (Nutzerregel): Preisvorschlag endet immer auf ,95.
            rec = pricing.round_up_to_cents(float(ki_rec["recommended_price_eur"]), cents)
        rec = rec or v["calc_recommended_eur"]
        # HARTE Untergrenze: nie unter die 20%-Marge-Schwelle (verlustfrei), auch wenn die
        # KI tiefer will. Senkung ist erlaubt, aber nur bis zum Boden.
        floor = v.get("price_floor_eur")
        if rec is not None and floor is not None and rec < floor:
            rec = pricing.round_up_to_cents(floor, cents)   # >= Boden, endet auf ,95
            v["clamped_to_floor"] = True
        v["recommended_price_eur"] = rec
        v["ki_reasoning"] = (ki_rec or {}).get("reasoning") or ""
        cur = v.get("current_price_eur")
        # Kennzeichnung Senkung vs. Anhebung – das ist der Kern von Nutzerwunsch #4.
        v["is_lowering"] = bool(rec is not None and cur and rec < float(cur))
        v["is_raising"] = bool(rec is not None and cur and rec > float(cur))
        # Marge zum empfohlenen Preis (Anzeige) – NUR aus echtem EK, nie geschaetzt.
        if v["recommended_price_eur"] and v["ek_eur"]:
            p = v["recommended_price_eur"]
            v["recommended_margin"] = round(
                (p - p * pricing.effective_fee_pct_for_listing(listing, settings=settings)
                 - pricing.ebay_fixed_fee(settings) - v["ek_eur"]) / p, 4)

    # Anzeigenrate-Empfehlung: NICHT mehr fix Basis+3% (=immer 13%), sondern die HÖCHSTE
    # Rate, die die Zielmarge noch traegt – gedeckelt. base_comm = implizite Provision
    # (Gesamtgebuehr ohne den Anzeigenanteil). Referenz: empfohlener Preis + teuerste
    # Varianten-EK (worst case). So gilt „eBay-Rate uebernehmen, sofern die Marge stimmt".
    base_comm = pricing.commission_pct(listing.category_name)   # kategorie-genaue Provision
    hard_cap = settings.ad_rate_hard_cap_pct
    ref_price = ref_ek = None
    for v in variants:
        if v.get("recommended_price_eur") and v.get("ek_eur"):
            if ref_ek is None or v["ek_eur"] > ref_ek:
                ref_ek, ref_price = v["ek_eur"], v["recommended_price_eur"]
    ad_margin_limited = False
    if ref_price and ref_ek:
        # Marge = 1 - (Provision + Anzeige) x 1,19 MwSt - (Fixbetrag_inkl_MwSt + EK)/Preis >= Ziel.
        # Nach der ROHEN Anzeigenrate aufloesen (die der Verkaeufer bei eBay eintraegt; eBay
        # besteuert sie dann): ad = [1 - Ziel - (fix + ek)/Preis]/1,19 - Provision.
        affordable = ((1 - settings.target_margin_pct
                       - (pricing.ebay_fixed_fee(settings) + ref_ek) / ref_price)
                      / pricing.fee_vat_factor(settings) - base_comm)
        rec_ad_frac = min(hard_cap, max(0.0, affordable))
        ad_margin_limited = affordable < settings.ebay_ad_rate_pct
        rec_ad = round(rec_ad_frac * 100, 1)
    else:
        rec_ad = round(min(hard_cap, settings.ebay_ad_rate_pct + 0.03) * 100, 1)
    # Bilder fuer die Titelbild-Auswahl. LIVE-Listing: die ECHTEN eBay-Bilder (die evtl.
    # falschen AliExpress-Quell-Bilder werden NICHT gezeigt). Entwurf/Mock: Quell-Bilder.
    imgs, img_source = await _fetch_listing_images(listing, product)
    _tip = ("Als Titelbild zieht ein helles, formatfüllendes Produktfoto ohne Text/Rahmen "
            "die meisten Klicks – Varianten-/Detailbilder besser dahinter.")
    if img_source == "ebay":
        img_note = f"{len(imgs)} Bilder aus deiner eBay-Anzeige (Titelbild zuerst). {_tip}"
    elif listing.ebay_item_id:
        # Live, aber eBay-Bilder gerade nicht abrufbar – NICHT „noch nicht live" behaupten.
        img_note = ("⚠ eBay-Bilder konnten gerade nicht geladen werden – die folgenden sind "
                    "Quell-Bilder und können von deiner Anzeige abweichen. Bitte erneut auf "
                    "Optimieren klicken, bevor du ein Titelbild setzt.")
    else:
        img_note = (f"{len(imgs)} Bilder aus der Quelle – noch nicht live auf eBay. {_tip}"
                    ) if imgs else ""
    return {
        "current_price_eur": float(listing.price_eur) if listing.price_eur is not None else None,
        "is_live": bool(listing.ebay_item_id),
        "image_candidates": imgs,
        "image_note": img_note,
        "image_source": img_source,
        "competitor_price_range": price_range,
        "competitor_samples": [{"title": o.get("title"), "price_eur": o.get("price_eur"),
                                "url": o.get("url")} for o in offers[:8] if o.get("price_eur")],
        "ad_rate_pct": round(settings.ebay_ad_rate_pct * 100, 1),
        "recommended_ad_rate_pct": rec_ad,
        "ad_rate_margin_limited": ad_margin_limited,
        "fee_pct": pricing.effective_fee_pct_for_listing(listing, settings=settings),
        "commission_pct": base_comm,
        "category_name": listing.category_name,
        "fixed_fee_eur": pricing.ebay_fixed_fee(settings),
        "target_margin": settings.target_margin_pct,
        "variants": variants,
        "push_recommendations": ki.get("push_recommendations") or [],
        "market_note": ki.get("market_note") or "",
    }


async def market_analysis(db: Session, *, listing_id: int) -> dict:
    """KI-Marktanalyse (propose-only) fuer EIN Listing – aendert nichts."""
    from app.models import Listing
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ValueError("Listing nicht gefunden")
    offers = await _fetch_competitor_offers(_real_ebay(), listing)
    market = await _compute_market(db, listing, offers)
    return {"listing_id": listing_id, "title": listing.title_seo, **market}


async def assess_images(db: Session, *, listing_id: int) -> dict:
    """KI-Bildbewertung (propose-only): bewertet die Bilder der eBay-Anzeige (bzw. Quelle)
    per Vision und schlaegt das beste TITELBILD vor. Aendert NICHTS – reine Entscheidungshilfe.

    Rueckgabe: {listing_id, image_source, best_index, note,
                images: [{url, is_main, kind, label?, score, reason, is_best}]}.
    """
    from app.models import Product
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ValueError("Listing nicht gefunden")
    product = db.get(Product, listing.product_id) if listing.product_id else None
    imgs, img_source = await _fetch_listing_images(listing, product)
    urls = [c.get("url") for c in imgs if c.get("url")]
    out_imgs = [dict(c) for c in imgs]
    if not urls:
        return {"listing_id": listing_id, "image_source": img_source, "images": out_imgs,
                "best_index": None, "note": "Keine Bilder zum Bewerten gefunden."}
    title = listing.title_seo or (product.title_raw if product else "") or ""
    try:
        assessed = await get_llm_client().assess_images(product_title=title, image_urls=urls)
    except Exception as exc:  # noqa: BLE001 – Bewertung darf den Panel-Aufruf nie sprengen
        logger.warning("assess_images failed",
                       extra={"listing_id": listing_id, "error": str(exc)[:150]})
        assessed = {"assessments": [], "best_index": None, "note": ""}
    by_idx = {a["index"]: a for a in (assessed.get("assessments") or [])}
    best = assessed.get("best_index")
    for i, item in enumerate(out_imgs):
        a = by_idx.get(i)
        item["score"] = a["score"] if a else None
        item["reason"] = (a.get("reason") if a else None) or None
        item["is_best"] = (best is not None and i == best)
    note = assessed.get("note") or ""
    if not by_idx:      # kein Ergebnis -> Nutzer klar sagen, dass die KI gerade nicht half
        note = note or "KI-Bildbewertung gerade nicht verfügbar – bitte später erneut versuchen."
    return {"listing_id": listing_id, "image_source": img_source, "images": out_imgs,
            "best_index": best, "note": note, "title": title}


def dismiss_cleanup(db: Session, *, listing_id: int, undo: bool = False) -> dict:
    """Aufraeum-Empfehlung verwerfen ('behalten') bzw. wieder aktivieren. Loescht NICHTS."""
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ValueError("Listing nicht gefunden")
    listing.cleanup_dismissed_at = None if undo else _now()
    db.commit()
    return {"listing_id": listing_id, "dismissed": not undo}


def dismiss_optimization(db: Session, *, listing_id: int, undo: bool = False) -> dict:
    """Optimierung 'so lassen' (verwerfen): der Artikel laeuft unveraendert weiter und
    verschwindet aus den Preis-/Titel-/Reaktivieren-Buckets. Aendert/loescht NICHTS am
    Listing selbst; ein offener KI-Titelvorschlag wird mit verworfen."""
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ValueError("Listing nicht gefunden")
    if undo:
        listing.opt_dismissed_at = None
    else:
        listing.opt_dismissed_at = _now()
        listing.optimization_suggestion = None
        if listing.optimization_status == "needs_review":
            listing.optimization_status = "reviewed"
    db.commit()
    return {"listing_id": listing_id, "dismissed": not undo}


async def cleanup_price_verdict(db: Session, *, listing_id: int) -> dict:
    """STUFE B (on-demand, 1 Browse-Call): prueft fuer EINEN Aufraeum-Kandidaten, ob er
    deutlich teurer als der Markt ist UND der Preis nicht unter den 20%-Boden gesenkt
    werden kann, um mitzuhalten. Nur Entscheidungshilfe – loescht/aendert NICHTS."""
    from app.services import pricing
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ValueError("Listing nicht gefunden")
    settings = get_settings()
    offers = await _fetch_competitor_offers(_real_ebay(), listing)
    stats = _price_stats([o["price_eur"] for o in offers if o.get("price_eur")])
    median = stats["median"] if stats else None
    cur = float(listing.price_eur) if listing.price_eur is not None else None
    floor = (pricing.price_floor(float(listing.cost_eur), category_name=listing.category_name,
                                 ad_rate_pct=getattr(listing, "ad_rate_pct", None), settings=settings)
             if listing.cost_eur is not None else None)
    overpriced = bool(cur and median and cur > median * (1 + settings.cleanup_price_premium_pct))
    # "Preis kann nicht runter": um den Markt (Median) zu erreichen, muesste man UNTER die
    # 20%-Marge-Grenze -> kein verlustfreier Weg, konkurrenzfaehig zu werden.
    cannot_lower = bool(median and floor and floor > median)
    recommend_delete = bool(overpriced and cannot_lower)
    if median is None:
        note = "Keine Konkurrenzpreise gefunden – kein Preis-Urteil moeglich."
    elif recommend_delete:
        note = (f"Deutlich teurer als der Markt (Median {median} €) und nicht verlustfrei "
                f"senkbar (Boden {floor} € > Median) – Loeschen erwaegen.")
    elif overpriced:
        note = (f"Teurer als der Markt (Median {median} €), aber senkbar bis {floor} € "
                f"(>=20% Marge) – erst Preis testen statt loeschen.")
    else:
        note = f"Preislich im Rahmen (Median {median} €) – nicht am Preis gescheitert."
    return {"listing_id": listing_id, "current_price_eur": cur,
            "competitor_median_eur": median, "competitor_count": stats["count"] if stats else 0,
            "price_floor_eur": floor, "overpriced": overpriced,
            "cannot_lower_enough": cannot_lower, "recommend_delete": recommend_delete,
            "note": note}


def suggest_volume_pricing(db: Session, *, listing_id: int) -> dict:
    """Multi-Buy-Rabatt-Empfehlung fuer EIN Listing (READ-ONLY, propose-only). Nutzt die
    schlechteste Variante (hoechster EK = niedrigste Marge), damit die Staffel fuer ALLE
    Varianten verlustfrei ist. Kategorie-genaue Gebuehr."""
    from app.models import Product
    from app.services import golive_service as gl, pricing, listing_match_service as lm
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ValueError("Listing nicht gefunden")
    settings = get_settings()
    fee_pct = pricing.effective_fee_pct_for_listing(listing, settings=settings)
    product = db.get(Product, listing.product_id) if listing.product_id else None
    # GELD-SICHERHEIT: Mengenrabatt NUR bei bestaetigter Quelle (System-Upload oder manuell
    # verifiziert). Unbestaetigte Bild-Matches koennten falsch/nicht mehr lieferbar sein ->
    # ein Rabatt darauf waere fatal (Verlust/Nicht-Erfuellung). Vorfall Kette 2026-07-07.
    if not lm.source_is_confirmed(product):
        return {"listing_id": listing_id, "recommended": False, "source_confirmed": False,
                "reason": "Quelle nicht bestätigt – erst Quelle prüfen/verifizieren, "
                          "dann Mengenrabatt möglich", "tiers": []}
    rows = gl.variant_price_rows(listing, product, settings)
    cand = [(r["ek_eur"], r["current_price_eur"], r.get("ae_price_eur")) for r in rows
            if r.get("ek_eur") and r.get("current_price_eur")]
    if not cand and listing.cost_eur and listing.price_eur:
        cand = [(float(listing.cost_eur), float(listing.price_eur), None)]
    if not cand:
        return {"listing_id": listing_id, "recommended": False,
                "reason": "keine EK/VK-Daten", "tiers": []}
    # Worst case = die Variante mit dem NIEDRIGSTEN Basis-Gewinn (nicht dem hoechsten EK):
    # eine billige Verlust-Variante (kleiner EK, aber VK unter Wasser) ist die bindende
    # Grenze fuer einen Rabatt, der fuer ALLE Varianten gilt (Nutzer-Fund IRAN-Kette 09.08.).
    fixed_fee = pricing.ebay_fixed_fee(settings)
    ek_row, vk, ae = min(cand, key=lambda t: t[1] - t[1] * fee_pct - fixed_fee - t[0])
    # GELD-SICHERHEIT: konservativste EK-Basis. Der real kalkulierte listing.cost_eur kann
    # HOEHER liegen als der aktuelle AE-Listenpreis (andere/ältere Variante) -> sonst wirken
    # die Margen optimistischer als die Realität und ein duenner Artikel wird faelschlich
    # empfohlen (Vorfall Motocross-Brille 09.07.: 13% Ist-Marge, aber als lohnend angezeigt).
    ek = max(ek_row, float(listing.cost_eur)) if listing.cost_eur is not None else ek_row
    # Marginale EK der Zusatz-Einheit (Bundle-Versand-Trick). Der Spar-Betrag (gesparter
    # AliExpress-Versand) ist eine Eigenschaft des AE-Preises, UNABHAENGIG vom absoluten
    # Kostenniveau: bundle_saving = voller AE-EK − marginaler AE-EK. Denselben absoluten
    # Betrag von der KONSERVATIVEN EK abziehen -> im Normalfall (ek == AE-EK) exakt die echte
    # marginale EK (Versand-Trick bleibt erhalten), bei abweichendem cost_eur konsistent
    # hochskaliert (nie unter 0, nie teurer als die volle Einheit).
    sov = float(listing.supplier_ship_eur) if listing.supplier_ship_eur is not None else None
    if ae is not None:
        from app.services.fast_shipping_service import variants_have_eu_warehouse as _vheu
        _loc = _vheu(getattr(listing, "product", None))
        ae_full = pricing.effective_cost(ae, settings=settings, ship_override=sov, local=_loc)
        ae_marg = pricing.marginal_unit_cost(ae, at_qty=2, settings=settings, ship_override=sov,
                                             local=_loc)
        bundle_saving = max(0.0, round(ae_full - ae_marg, 2))
        ek_extra = round(max(0.0, min(ek, ek - bundle_saving)), 2)
    else:
        ek_extra = ek        # kein AE-Rohpreis -> keine Bundle-Ersparnis annehmen (konservativ)
    plan = pricing.volume_pricing_plan(vk, ek, fee_pct=fee_pct, ek_extra=ek_extra, settings=settings)
    return {"listing_id": listing_id, "title": listing.title_seo, "source_confirmed": True,
            "category_name": listing.category_name, "fee_pct": fee_pct,
            "ek_eur": round(ek, 2), "ek_extra_eur": ek_extra, "vk_eur": round(vk, 2), **plan}


def list_volume_pricing_candidates(db: Session) -> dict:
    """Alle aktiven Listings durchrechnen (read-only, keine eBay-Calls) und die als
    Mengenrabatt-KANDIDATEN zurueckgeben, bei denen sich Multi-Buy lohnt (Marge hoch genug,
    jede Staffel bringt echten Mehrgewinn). Sortiert nach Mehrgewinn je Zusatz-Stueck."""
    listings = db.scalars(select(Listing).where(
        Listing.listing_status == "active",
        Listing.price_eur.isnot(None), Listing.cost_eur.isnot(None))).all()
    out = []
    for l in listings:
        try:
            r = suggest_volume_pricing(db, listing_id=l.id)
        except Exception:  # noqa: BLE001 – ein Listing darf den Lauf nicht stoppen
            continue
        active = bool(l.volume_promotion_id)                       # Mengenrabatt schon auf eBay aktiv?
        recommended = bool(r.get("recommended") and r.get("tiers"))
        # Anzeigen: empfohlene Kandidaten – UND bereits AKTIVE Promos, selbst wenn die Marge
        # inzwischen zu duenn ist (sonst verschwinden sie aus der Liste und koennen nicht mehr
        # verwaltet/entfernt werden – Vorfall Motocross-Brille 09.07.).
        if not recommended and not active:
            continue
        out.append({
            "listing_id": l.id, "title": l.title_seo,
            # Icon-Fallback: nicht jedes Listing hat image_url -> erstes Produktbild.
            "image": l.image_url or next(iter(
                (l.product.images if getattr(l, "product", None) else None) or []), None),
            "ebay_item_id": l.ebay_item_id, "category_name": r.get("category_name"),
            "vk_eur": r.get("vk_eur"), "ek_eur": r.get("ek_eur"),
            "ek_extra_eur": r.get("ek_extra_eur"),
            "margin_1": r.get("margin_1"), "profit_1_eur": r.get("profit_1_eur"),
            # Aktiv-aber-nicht-empfohlen: all_tiers zeigen, damit die laufende Staffel sichtbar
            # bleibt; sonst die empfohlenen Staffeln.
            "tiers": r.get("tiers") or (r.get("all_tiers") if active else []) or [],
            "recommended": recommended,
            "active": active,
            "warn": (("⚠ BASIS-VERLUST — Rabatt wird beim nächsten täglichen Lauf automatisch "
                      "entfernt; bitte Preis fixen")
                     if (active and r.get("profit_1_eur") is not None
                         and r["profit_1_eur"] <= 0)
                     else (None if recommended else
                           (r.get("reason") or "Marge inzwischen zu dünn – Mengenrabatt überdenken"))),
        })
    # Schon aktive Mengenrabatte nach UNTEN (Nutzerwunsch) – oben nur, was noch keinen hat.
    # Innerhalb jeder Gruppe: groesster Mehrgewinn je Zusatz-Stueck zuerst.
    out.sort(key=lambda x: (bool(x["active"]),
                            -(x["tiers"][0]["db_extra_eur"] if x["tiers"] else 0)))
    return {"count": len(out), "candidates": out}


async def activate_volume_pricing(db: Session, *, listing_id: int) -> dict:
    """Aktiviert den EMPFOHLENEN Mengenrabatt auf eBay (Volume Pricing). Ausgeloest per
    manuellem Klick ODER automatisch beim Livegang neuer Listings (Nutzer-Entscheid 08.08.,
    Schalter multibuy_auto_activate_on_publish). Nie im Pauschal-Batch ueber Bestand."""
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ValueError("Listing nicht gefunden")
    if not listing.ebay_item_id:
        raise ValueError("Listing ist nicht live auf eBay")
    rec = suggest_volume_pricing(db, listing_id=listing_id)
    if not rec.get("recommended") or not rec.get("tiers"):
        raise ValueError(rec.get("reason") or "Fuer dieses Listing lohnt sich kein Mengenrabatt")
    tiers = [(t["qty"], t["discount_pct"]) for t in rec["tiers"]]
    ebay = _real_ebay()
    with task_log(db, task_type="volume_pricing", reference_id=listing_id) as tl:
        promo_id = await ebay.create_volume_pricing(
            listing.ebay_item_id, tiers=tiers,
            name=f"Mengenrabatt {(listing.title_seo or listing.ebay_item_id)[:60]}")
        listing.volume_promotion_id = promo_id
        tl.result_data = {"promotion_id": promo_id, "tiers": tiers}
    db.commit()
    return {"listing_id": listing_id, "promotion_id": promo_id, "tiers": rec["tiers"],
            "message": "Mengenrabatt auf eBay aktiviert."}


async def activate_all_volume_pricing(db: Session) -> dict:
    """Alle EMPFOHLENEN, noch NICHT aktiven Mengenrabatte auf eBay aktivieren – EIN
    Nutzer-Klick (mit Bestaetigung im UI), Geld-/Aussenaktion. Aktiviert je Kandidat
    einzeln ueber den bestehenden Geld-Pfad; Erfolge/Fehler werden gesammelt."""
    cands = list_volume_pricing_candidates(db)["candidates"]
    todo = [c for c in cands if not c.get("active")]
    activated, errors = [], []
    for c in todo:
        try:
            await activate_volume_pricing(db, listing_id=c["listing_id"])
            activated.append(c["listing_id"])
        except Exception as exc:  # noqa: BLE001 – ein Fehler stoppt den Rest nicht
            errors.append({"listing_id": c["listing_id"], "error": str(exc)[:160]})
    return {"activated": len(activated), "failed": len(errors),
            "total": len(todo), "errors": errors[:20]}


async def deactivate_volume_pricing(db: Session, *, listing_id: int) -> dict:
    """Aktiven Mengenrabatt wieder von eBay entfernen (Zuruecknehmen). Mit Audit-Log."""
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ValueError("Listing nicht gefunden")
    if not listing.volume_promotion_id:
        raise ValueError("Kein aktiver Mengenrabatt fuer dieses Listing")
    _pid = listing.volume_promotion_id
    with task_log(db, task_type="volume_pricing_remove", reference_id=listing_id) as tl:
        await _real_ebay().delete_volume_pricing(_pid)
        listing.volume_promotion_id = None
        tl.result_data = {"promotion_id": _pid}
    db.commit()
    return {"listing_id": listing_id, "message": "Mengenrabatt entfernt."}


async def auto_remove_unprofitable_multibuy(db: Session, *, limit: int = 100,
                                            respect_toggle: bool = True) -> dict:
    """AUTOMATIK: aktive Mengenrabatte entfernen, wenn die BASIS bereits Verlust macht
    (profit_1 <= 0; Nutzer-Entscheid 09.08., Fall IRAN-Kette) ODER keine Staffel mehr
    echten Mehrgewinn bringt. NUR Entfernen, nie Hinzufuegen. Per
    ``settings.multibuy_auto_remove`` abschaltbar (``respect_toggle``);
    ein manueller Klick uebergeht den Schalter (explizite Zustimmung).

    GELD-SICHERHEIT (Aussen-/Live-Aktion): rechnet je Listing frisch mit echtem, konservativem EK.
    Fehlt ein gueltiger Plan (unbestaetigte Quelle / keine EK-Daten -> leere ``all_tiers``), wird
    NICHT angefasst. Ein Fehler stoppt den Rest nicht; jede Entfernung ist per task_log auditiert."""
    s = get_settings()
    if respect_toggle and not s.multibuy_auto_remove:
        return {"enabled": False, "checked": 0, "removed": 0, "removed_ids": [], "errors": []}
    # Studio-Angebote sind hier tabu: die Staffel wuerde live und ohne Rueckfrage
    # entfernt, obwohl die Kalkulation eines Print-on-Demand-Artikels ganz anders
    # aussieht als die eines Dropshipping-Artikels.
    from app.studio import exclude_studio
    actives = db.scalars(exclude_studio(
        select(Listing).where(
            Listing.listing_status == "active",
            Listing.volume_promotion_id.isnot(None)),
        db,
    )).all()
    removed, errors, checked = [], [], 0
    for l in actives:
        if len(removed) >= limit:
            logger.warning("multibuy auto-remove: Limit erreicht – Rest naechster Lauf",
                           extra={"limit": limit})
            break
        try:
            rec = suggest_volume_pricing(db, listing_id=l.id)
        except Exception as exc:  # noqa: BLE001 – ein Listing darf den Lauf nicht stoppen
            errors.append({"listing_id": l.id, "error": f"recompute: {str(exc)[:120]}"})
            continue
        checked += 1
        all_tiers = rec.get("all_tiers") or []
        # Entfernen, wenn ein gueltiger Plan existiert UND (a) die BASIS schon Verlust macht
        # ODER (b) keine Staffel echten Mehrgewinn bringt. (a) ist neu (Nutzer-Fund IRAN-Kette
        # 09.08.): ein Verlust-Listing darf NIE einen Kaufanreiz fuer MEHR Einheiten tragen —
        # jede zusaetzlich verkaufte Einheit vergroessert den Schaden. Fehlender Plan /
        # unbestaetigte Quelle (all_tiers leer) -> weiterhin nicht anfassen.
        base_profit = rec.get("profit_1_eur")
        base_loss = base_profit is not None and base_profit <= 0
        if not all_tiers or (not base_loss and any(t.get("ok") for t in all_tiers)):
            continue
        try:
            await deactivate_volume_pricing(db, listing_id=l.id)
            removed.append(l.id)
            logger.info("multibuy auto-remove: entfernt",
                        extra={"listing_id": l.id, "reason": rec.get("reason"),
                               "margin_1": rec.get("margin_1")})
        except Exception as exc:  # noqa: BLE001 – ein Live-Fehler stoppt den Rest nicht
            errors.append({"listing_id": l.id, "error": str(exc)[:160]})
    return {"enabled": True, "checked": checked, "removed": len(removed),
            "removed_ids": removed[:50], "errors": errors[:20]}


async def optimize_one(db: Session, *, listing_id: int) -> dict:
    """ON-DEMAND „Optimieren": Titel-Vorschlag UND Marktanalyse in EINEM Schritt
    (eine Konkurrenz-Suche fuer beides). Rein propose-only – nichts wird angewandt.
    """
    from app.models import Listing
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ValueError("Listing nicht gefunden")
    ebay = _real_ebay()
    llm = get_llm_client()
    offers = await _fetch_competitor_offers(ebay, listing)
    competitor_titles = [o["title"] for o in offers if o.get("title")][:10]
    suggestion = await _suggest_title_for(llm, listing, competitor_titles, _bestseller_titles(db, listing))
    if suggestion:
        listing.optimization_suggestion = suggestion
        listing.optimization_status = "needs_review"
        listing.last_optimization_date = _now()   # Vorschlags-Zeitpunkt (NICHT das 14-Tage-Hide)
        db.commit()
    market = await _compute_market(db, listing, offers)
    return {"listing_id": listing_id, "title": listing.title_seo,
            "suggestion": suggestion, **market}


def _capture_optimization(listing) -> None:
    """Listing als optimiert markieren (14-Tage-Hide) + VORHER-Momentaufnahme der Kennzahlen
    sichern, damit der Nachverfolgungs-Reiter Vorher/Nachher vergleichen kann."""
    from sqlalchemy.orm.attributes import flag_modified
    listing.optimization_status = "optimized"
    listing.optimization_stage = max(listing.optimization_stage or 0, 1)
    listing.last_optimization_date = _now()
    listing.optimized_at = _now()
    listing.opt_snapshot = {
        "clicks": int(listing.clicks_week or 0),
        "impressions": int(listing.impressions_week or 0),
        "sales": int(listing.sales_total or 0),
        "price_eur": float(listing.price_eur) if listing.price_eur is not None else None,
        "at": _now().isoformat(),
    }
    flag_modified(listing, "opt_snapshot")


async def apply_optimization(db: Session, *, listing_id: int, title: str | None = None,
                             prices: dict | None = None,
                             ad_rate_pct: float | None = None) -> dict:
    """„Alle Aenderungen annehmen": Titel + Variantenpreise + Anzeigentarif auf einmal
    uebernehmen (jeweils verifiziert/ueber die bestehenden Geld-Pfade). Markiert das
    Listing als optimiert (optimized_at = jetzt) -> 14 Tage aus der Kandidatenliste.
    Jede Teil-Aktion ist optional; Fehler je Teil werden gesammelt (Rest laeuft weiter).
    """
    from app.models import Listing
    from app.services import golive_service as gl
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ValueError("Listing nicht gefunden")
    applied, errors = [], []

    if title and title.strip() and title.strip() != (listing.title_seo or "").strip():
        clean = _clean_suggested_title(title, listing.title_seo) or title.strip()[:80]
        try:
            if listing.ebay_item_id:
                ebay = _real_ebay()
                await retry_async(lambda: ebay.revise_item_title(listing.ebay_item_id, clean),
                                  label="ebay_title")
            listing.title_seo = clean
            applied.append("Titel")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Titel: {str(exc)[:120]}")

    if prices:
        try:
            r = await gl.apply_variant_prices(
                db, listing_id=listing_id,
                price_by_sku={str(k): float(v) for k, v in prices.items() if v})
            applied.append(f"{len(r.get('updated') or prices)} Preis(e)")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Preise: {str(exc)[:120]}")

    if ad_rate_pct is not None and listing.ebay_item_id:
        try:
            ebay = _real_ebay()
            _bid = float(ad_rate_pct) / 100.0
            await ebay.set_ad_rate([listing.ebay_item_id], bid_pct=_bid)
            # Echte Rate sofort lokal merken -> Gebuehrenkalkulation nutzt sie ab jetzt
            # (statt bis zum naechsten taeglichen sync_ad_rates auf der Pauschale zu bleiben).
            listing.ad_rate_pct = _bid
            applied.append(f"Anzeigentarif {ad_rate_pct}%")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Anzeigentarif: {str(exc)[:120]}")

    # Optimierung abschliessen: Vorschlag raus, optimiert markieren (14-Tage-Hide) +
    # Vorher-Snapshot fuer die Nachverfolgung.
    listing.optimization_suggestion = None
    _capture_optimization(listing)
    db.commit()
    return {"listing_id": listing_id, "applied": applied, "errors": errors,
            "hidden_days": get_settings().opt_hide_days}


async def refresh_click_data(db: Session) -> dict:
    """Klick-/Impressions-Kennzahlen aller aktiven Listings auffrischen – ueber EINEN
    Sammel-Traffic-Report (nicht 389 Einzelabrufe; die sprengen die Analytics-Quota).
    Listings ohne Report-Eintrag hatten 0 Traffic -> auf 0 setzen (nur wenn der Report
    ueberhaupt Daten lieferte; bei leerem/fehlgeschlagenem Report bleibt alles wie es war).
    """
    ebay = _real_ebay()
    settings = get_settings()
    error = None
    listings = db.scalars(
        select(Listing).where(Listing.listing_status == "active")).all()
    # Gezielt NUR unsere Listings per ID abfragen (chunked, 200/Call) – der frueher
    # genutzte ungefilterte Report war auf 200 Zeilen gedeckelt und liess die meisten
    # Listings verschwinden (-> faelschlich auf 0 gesetzt). s. get_all_listing_analytics.
    item_ids = [str(lst.ebay_item_id) for lst in listings if lst.ebay_item_id]
    try:
        report = await ebay.get_all_listing_analytics(
            item_ids=item_ids, days=settings.opt_zero_click_days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("traffic report failed", extra={"error": str(exc)[:160]})
        report = {}
        error = str(exc)[:200]
    refreshed = 0
    for lst in listings:
        a = report.get(str(lst.ebay_item_id)) if lst.ebay_item_id else None
        if a is not None:
            lst.impressions_week = a.impressions
            lst.clicks_week = a.clicks
            refreshed += 1
        elif report:   # Report da (gezielt per ID) -> Listing fehlt = echt 0 Traffic
            lst.impressions_week = 0
            lst.clicks_week = 0
    db.commit()
    result = {"refreshed": refreshed, "total": len(listings), "report_size": len(report),
              "error": error, "finished_at": _now().isoformat()}
    set_last_refresh(result)
    return result


async def accept_suggestion(db: Session, *, listing_id: int) -> dict:
    """Vorschlag FREIGEBEN: Titel via Trading-API auf eBay setzen, dann lokal
    uebernehmen und Vorschlag loeschen. Schlaegt der eBay-Push fehl, bleibt der
    Vorschlag erhalten (kein stiller Teil-Erfolg)."""
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ValueError("Listing nicht gefunden")
    new_title = _clean_suggested_title(listing.optimization_suggestion, None)
    if not new_title:
        # Kaputter/leerer Vorschlag -> verwerfen statt anwenden.
        listing.optimization_suggestion = None
        listing.optimization_status = "reviewed"
        db.commit()
        raise ValueError("Kein gueltiger Titelvorschlag vorhanden")
    ebay = _real_ebay()
    with task_log(db, task_type="optimize", reference_id=listing_id) as tl:
        await retry_async(
            lambda: ebay.revise_item_title(listing.ebay_item_id or f"item_{listing_id}",
                                           new_title),
            label="ebay_title",
        )
        listing.title_seo = new_title
        listing.optimization_suggestion = None
        _capture_optimization(listing)   # optimiert markieren + Vorher-Snapshot
        tl.result_data = {"applied_title": new_title}
        db.commit()
    return {"listing_id": listing_id, "title": new_title, "status": "optimized"}


def reject_suggestion(db: Session, *, listing_id: int) -> dict:
    """Vorschlag VERWERFEN: Vorschlag loeschen, Titel bleibt unveraendert."""
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ValueError("Listing nicht gefunden")
    listing.optimization_suggestion = None
    listing.optimization_status = "reviewed"
    listing.last_optimization_date = _now()
    db.commit()
    return {"listing_id": listing_id, "status": "reviewed"}


def cleanup_candidate_ids(db: Session) -> list[int]:
    """IDs der aktuellen Aufraeum-Kandidaten (nie verkauft, 0 Klicks, <= Impressions-Schwelle,
    alt genug bzw. optimiert-und-trotzdem-tot). Einzige Wahrheitsquelle fuer die Massen-Aktion."""
    buckets = (list_optimization_buckets(db).get("buckets") or {}).get("cleanup") or []
    return [int(i["listing_id"]) for i in buckets if i.get("listing_id") is not None]


def _cleanup_still_valid(db: Session, listing_id: int) -> tuple[bool, str]:
    """ZWEITE, unabhaengige Pruefung DIREKT vor dem Beenden (nicht der Bucket-Liste vertrauen).

    Geld-/Datensicherheit: ein Listing darf NUR beendet werden, wenn es nachweislich
    nie verkauft wurde, 0 Klicks und hoechstens ``cleanup_max_impressions`` Impressionen
    hat und noch aktiv ist. Jede Abweichung -> nicht anfassen (fail-closed).
    """
    from app.models import Sale
    s = get_settings()
    l = db.get(Listing, listing_id)
    if l is None:
        return False, "Listing nicht gefunden"
    if l.listing_status != "active":
        return False, f"nicht aktiv ({l.listing_status})"
    if (l.sales_total or 0) > 0:
        return False, f"hat {l.sales_total} Verkauf/Verkaeufe (eBay-Zaehler)"
    if db.scalar(select(func.count()).select_from(Sale).where(Sale.listing_id == listing_id)) or 0:
        return False, "hat Verkaufszeilen in der Datenbank"
    if (l.clicks_week or 0) > 0:
        return False, f"hat {l.clicks_week} Klicks"
    if (l.impressions_week or 0) > max(0, s.cleanup_max_impressions):
        return False, f"hat {l.impressions_week} Impressionen (> {s.cleanup_max_impressions})"
    return True, "ok"


async def end_cleanup_candidates(db: Session, *, listing_ids: list[int] | None = None,
                                 limit: int = 100) -> dict:
    """Aufraeum-Kandidaten in EINEM Rutsch auf eBay beenden (nur auf ausdrueckliche Nutzer-Aktion).

    Sicherungen: (1) die Liste wird SERVERSEITIG neu berechnet – eine mitgeschickte ID-Liste
    kann sie nur EINSCHRAENKEN, nie erweitern; (2) je Listing eine zweite unabhaengige Pruefung
    unmittelbar vor dem Beenden (``_cleanup_still_valid``); (3) Deckel ``limit``; (4) ein Fehler
    stoppt den Lauf nicht. Der DB-Datensatz bleibt erhalten (Status 'ended').
    """
    from app.services import golive_service
    valid = cleanup_candidate_ids(db)
    if listing_ids:
        wanted = {int(x) for x in listing_ids}
        ids = [i for i in valid if i in wanted]          # nur Schnittmenge – nie erweitern
    else:
        ids = list(valid)
    ids = ids[:max(1, limit)]
    ended: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    for lid in ids:
        ok, why = _cleanup_still_valid(db, lid)
        if not ok:
            skipped.append({"listing_id": lid, "grund": why})
            continue
        title = (db.get(Listing, lid).title_seo or "")[:60]
        try:
            res = await golive_service.end_listing_live(db, listing_id=lid)
            ended.append({"listing_id": lid, "titel": title, "detail": res.get("detail")})
        except Exception as exc:  # noqa: BLE001 – ein Fehler stoppt den Lauf nicht
            db.rollback()
            logger.warning("cleanup end failed", extra={"listing_id": lid, "error": str(exc)[:200]})
            failed.append({"listing_id": lid, "titel": title, "fehler": str(exc)[:200]})
    return {"beendet": ended, "uebersprungen": skipped, "fehlgeschlagen": failed,
            "n_beendet": len(ended), "n_uebersprungen": len(skipped), "n_fehlgeschlagen": len(failed)}
