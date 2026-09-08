"""3€-Versand-Migration (einmaliger Lauf): alle Artikel mit Zuschlags-Policy
+3,00€ je Variante und Versand auf 'Kostenloser Versand 7 Tage Bearbeitung'.

Summenneutral fuer den Kaeufer. Jeder Artikel wird per Read-back VERIFIZIERT;
bei Policy-Fehlern werden die Preise zurueckgerollt. Fortschritt/Fehler in
logs/migration_3eur.log, Ergebnis als JSON auf stdout.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.integrations.ebay import RealEbayClient  # noqa: E402
from app.models import Listing  # noqa: E402
from app.services import ebay_import_service  # noqa: E402

LOG = Path("./logs/migration_3eur.log")


def log(msg: str) -> None:
    print(msg, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


async def main() -> None:
    ebay = RealEbayClient(get_settings())
    db = SessionLocal()
    free_id = str(get_settings().ebay_fulfillment_policy_id)
    listings = db.scalars(select(Listing).where(
        Listing.listing_status == "active",
        Listing.shipping_policy_id.isnot(None))).all()
    targets = [l.id for l in listings
               if "kostenlos" not in (l.shipping_policy_name or "").lower()
               and str(l.shipping_policy_id) != free_id]
    log(f"START Migration: {len(targets)} Artikel (+3,00€ je Variante, Versand kostenlos)")

    ok, failed = 0, []
    for i, lid in enumerate(targets, 1):
        try:
            r = await ebay_import_service.migrate_listing_plus3_free_shipping(
                db, listing_id=lid, ebay=ebay)
            ok += 1
            log(f"  ok {i}/{len(targets)} listing={lid} varianten={r['variants']} "
                f"neu_max={r['new_max_eur']}€")
        except Exception as exc:  # noqa: BLE001 – Einzel-Fehler stoppt den Lauf nicht
            db.rollback()
            failed.append({"listing_id": lid, "error": str(exc)[:200]})
            log(f"  FEHLER {i}/{len(targets)} listing={lid}: {str(exc)[:160]}")
        await asyncio.sleep(0.6)

    summary = {"targets": len(targets), "ok": ok, "failed": failed}
    log("FERTIG " + json.dumps(summary, ensure_ascii=False))
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
