"""Bestehende Listings mit 925/Echtsilber/Sterlingsilber finden und auf „versilbert" korrigieren.

Nutzerregel 28.07.: kein neuer Button – die betroffenen (wenigen) Listings werden beim App-Start
automatisch einmal korrigiert (``startup_sweep``). Bloßes „Silber" ist erlaubt und bleibt.
Korrektur läuft über ``golive_service.update_listing_live`` (Read-back-verifiziert; Preis unberührt).
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Listing
from app.precious_metal_filter import (REPLACEMENT, contains_precious_metal_claim,
                                       correct_material_specs, sanitize_description,
                                       sanitize_title)
from app.services import golive_service

logger = logging.getLogger("app.services.precious_metal")


def _material_of(specs: dict) -> str | None:
    for k in ("Material", "Metall", "Werkstoff", "Metalltyp"):
        if specs.get(k):
            return str(specs[k])
    return None


def _proposal(listing: Listing, *, replacement: str = REPLACEMENT) -> dict | None:
    title = listing.title_seo or ""
    desc = listing.description or ""
    specs = listing.item_specifics if isinstance(listing.item_specifics, dict) else {}
    new_title, t_ch = sanitize_title(title, replacement=replacement)
    new_desc, d_ch = sanitize_description(desc, replacement=replacement)
    new_specs, s_ch = correct_material_specs(specs, replacement=replacement)
    if not (t_ch or d_ch or s_ch):
        return None
    return {
        "listing_id": listing.id, "ebay_item_id": listing.ebay_item_id,
        "image_url": listing.image_url,
        "title": title, "proposed_title": new_title, "title_changed": t_ch,
        "material": _material_of(specs), "proposed_material": _material_of(new_specs),
        "material_changed": s_ch, "desc_changed": d_ch,
    }


def scan_precious_metal_listings(db: Session, *, replacement: str = REPLACEMENT,
                                 include_drafts: bool = True) -> dict:
    """LESE-Analyse: Listings mit 925/Echtsilber/Sterlingsilber + Vorschlag. Ändert nichts."""
    statuses = ["active", "draft"] if include_drafts else ["active"]
    rows: list[dict] = []
    for listing in db.scalars(select(Listing).where(Listing.listing_status.in_(statuses))):
        specs = listing.item_specifics if isinstance(listing.item_specifics, dict) else {}
        if not (contains_precious_metal_claim(listing.title_seo or "")
                or contains_precious_metal_claim(listing.description or "")
                or contains_precious_metal_claim(*[str(v) for v in specs.values()])):
            continue
        prop = _proposal(listing, replacement=replacement)
        if prop is not None:
            rows.append(prop)
    return {"count": len(rows), "replacement": replacement, "listings": rows}


async def apply_precious_metal_fix(db: Session, *, listing_ids: list[int],
                                   replacement: str = REPLACEMENT) -> dict:
    """Nur die übergebenen Listings korrigieren (Titel/Beschreibung/Material). Live-Listings gehen
    über den verifizierten eBay-Push; Preis wird NICHT verändert. Fehler stoppen den Lauf nicht."""
    applied: list[dict] = []
    partial: list[dict] = []
    failed: list[dict] = []
    skipped: list[int] = []
    for lid in listing_ids:
        listing = db.get(Listing, lid)
        if listing is None:
            failed.append({"listing_id": lid, "error": "Listing nicht gefunden"})
            continue
        if _proposal(listing, replacement=replacement) is None:
            skipped.append(lid)
            continue
        new_title, _ = sanitize_title(listing.title_seo or "", replacement=replacement)
        new_desc, _ = sanitize_description(listing.description or "", replacement=replacement)
        new_specs, _ = correct_material_specs(
            listing.item_specifics if isinstance(listing.item_specifics, dict) else {},
            replacement=replacement)
        try:
            res = await golive_service.update_listing_live(
                db, listing_id=lid, title=new_title, description=new_desc,
                item_specifics=(new_specs or None))   # leer -> None; Preis unberührt
            entry = {"listing_id": lid, "changed": res.get("changed"),
                     "pushed_to_ebay": res.get("pushed_to_ebay")}
            if res.get("warnings"):
                entry["warnings"] = res.get("warnings")
                partial.append(entry)                 # Teil-Erfolg (einzelne SKU abgelehnt) -> prüfen
            else:
                applied.append(entry)
        except Exception as exc:  # noqa: BLE001 – ein Fehler stoppt den Lauf nicht
            logger.warning("precious-metal fix failed", extra={"listing_id": lid, "error": str(exc)[:200]})
            failed.append({"listing_id": lid, "error": str(exc)[:200]})
    return {"applied": applied, "partial": partial, "failed": failed, "skipped": skipped,
            "n_applied": len(applied), "n_partial": len(partial), "n_failed": len(failed)}


_REGEN_INSTRUCTION = (
    "Dieser Artikel ist VERSILBERTER Modeschmuck (Edelstahl mit Silberauflage), KEIN Massivsilber. "
    "Entferne im Titel UND in der Beschreibung ALLE Echtheits-/Feingehalts-Behauptungen restlos: "
    "925, Sterling, Sterlingsilber, Echtsilber, 'echtes Silber/Edelmetall', Echtschmuck, jede Angabe "
    "zu 'Stempel' oder 'Zertifikat'. Nenne das Material 'versilbert' (oder 'versilberter Edelstahl'). "
    "Formuliere den TITEL SEO-optimiert: das Produkt-Keyword zuerst, dann die wichtigsten Merkmale, "
    "max. 80 Zeichen, keine Wort-Dopplungen. Schreibe die BESCHREIBUNG sauber, kohärent und "
    "verkaufsstark; behalte echte Fakten (Maße, Gewicht, Farbe) bei, erfinde nichts."
)


def _needs_regen(listing: Listing, *, replacement: str = REPLACEMENT) -> bool:
    """Betroffen: enthält noch eine Verbots-Behauptung ODER wurde vom alten Wort-Ersetzer verhunzt
    (Titel beginnt mit 'versilbert' = fehlende SEO-Wortstellung)."""
    specs = listing.item_specifics if isinstance(listing.item_specifics, dict) else {}
    if contains_precious_metal_claim(listing.title_seo or "", listing.description or "",
                                     *[str(v) for v in specs.values()]):
        return True
    return (listing.title_seo or "").strip().lower().startswith(replacement.lower() + " ")


async def regenerate_content(db: Session, *, listing_id: int) -> dict:
    """Titel + Beschreibung EINES Listings per KI neu formulieren (SEO-Titel, saubere Beschreibung,
    keine Echtheits-/Stempel-/Zertifikat-Behauptungen) und live setzen (Preis unberührt)."""
    from app.integrations import get_llm_client
    from app.spec_filter import strip_forbidden_specs
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ValueError("Listing nicht gefunden")
    old_title = listing.title_seo
    res = await get_llm_client().revise_listing(
        instruction=_REGEN_INSTRUCTION,
        current_title=listing.title_seo or "",
        current_description=listing.description or "",
        current_specifics=strip_forbidden_specs(listing.item_specifics or {}),
        current_category=listing.category_id)
    title = (res.get("title_seo") or listing.title_seo or "")[:80]
    desc = res.get("description") or listing.description or ""
    specs = strip_forbidden_specs(res.get("item_specifics") or listing.item_specifics or {})
    # Sicherheitsnetz: Filter erneut drüber (die KI darf keine Behauptung durchlassen).
    title, _ = sanitize_title(title)
    desc, _ = sanitize_description(desc)
    await golive_service.update_listing_live(
        db, listing_id=listing_id, title=title, description=desc, item_specifics=(specs or None))
    return {"listing_id": listing_id, "old_title": old_title, "new_title": title}


def count_affected(db: Session, *, include_drafts: bool = False) -> int:
    """Schnelle Zählung betroffener Listings (nur DB/Regex, keine KI/eBay)."""
    statuses = ["active", "draft"] if include_drafts else ["active"]
    return sum(1 for l in db.scalars(select(Listing).where(Listing.listing_status.in_(statuses)))
               if _needs_regen(l))


async def regenerate_affected_bg(*, limit: int = 3) -> None:
    """Häppchen im HINTERGRUND neu formulieren (eigene Session) – der HTTP-Request kehrt sofort
    zurück, damit er nicht auf die langsamen KI-/eBay-Calls wartet (Gateway-Timeout)."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        res = await regenerate_affected(db, limit=limit)
        logger.info("silver regen batch", extra={"done": res["n_done"], "failed": res["n_failed"],
                    "remaining": res["remaining"]})
    except Exception as exc:  # noqa: BLE001
        logger.warning("silver regen bg failed", extra={"error": str(exc)[:200]})
    finally:
        db.close()


async def regenerate_affected(db: Session, *, limit: int = 3, include_drafts: bool = False) -> dict:
    """Die nächsten ``limit`` betroffenen Listings per KI neu formulieren (kontrollierte Häppchen,
    dazwischen prüfbar). Fehler stoppen den Lauf nicht."""
    statuses = ["active", "draft"] if include_drafts else ["active"]
    affected = [l for l in db.scalars(select(Listing).where(Listing.listing_status.in_(statuses)))
                if _needs_regen(l)]
    batch = affected[:max(1, limit)]
    done: list[dict] = []
    failed: list[dict] = []
    for l in batch:
        try:
            done.append(await regenerate_content(db, listing_id=l.id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("regen failed", extra={"listing_id": l.id, "error": str(exc)[:200]})
            failed.append({"listing_id": l.id, "error": str(exc)[:200]})
    return {"total_affected": len(affected), "remaining": max(0, len(affected) - len(batch)),
            "done": done, "failed": failed, "n_done": len(done), "n_failed": len(failed)}


async def sweep_and_fix(db: Session, *, limit: int = 300) -> dict:
    """Alle betroffenen Listings finden und korrigieren (idempotent). Für den Auto-Lauf beim Start."""
    scan = scan_precious_metal_listings(db)
    ids = [r["listing_id"] for r in scan["listings"]][:max(1, limit)]
    if not ids:
        return {"found": 0, "n_applied": 0, "n_failed": 0}
    res = await apply_precious_metal_fix(db, listing_ids=ids)
    return {"found": scan["count"], **res}


async def startup_sweep(*, limit: int = 300) -> None:
    """Einmal-Korrektur beim App-Start (eigene DB-Session, best-effort, blockiert den Start nicht).
    Idempotent -> nach der ersten Korrektur findet der Scan nichts mehr (keine weiteren eBay-Writes)."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        res = await sweep_and_fix(db, limit=limit)
        if res.get("found"):
            logger.info("silver backfill", extra={"found": res.get("found"),
                        "applied": res.get("n_applied"), "failed": res.get("n_failed"),
                        "partial": res.get("n_partial")})
    except Exception as exc:  # noqa: BLE001 – Backfill darf den Start nie stören
        logger.warning("silver backfill failed", extra={"error": str(exc)[:200]})
    finally:
        db.close()
