"""Demodaten anlegen, damit die Endpoints sofort etwas zeigen.

Aufruf:  py scripts/seed.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal, init_db  # noqa: E402
from app.models import BankTransaction, Listing, Product  # noqa: E402


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        if db.query(Listing).count() > 0:
            print("Seed: es existieren bereits Listings – nichts zu tun.")
            return

        for i in range(1, 6):
            product = Product(
                aliexpress_url=f"https://de.aliexpress.com/item/demo{i}.html",
                aliexpress_id=f"100{i}",
                title_raw=f"Demo Produkt {i} 中国直邮",
                price_cny=Decimal("49.90"),
                images=[f"https://img.example/demo{i}_0.jpg"],
            )
            db.add(product)
            db.flush()
            db.add(Listing(
                product_id=product.id,
                ebay_item_id=f"20000000{i}",
                title_seo=f"Demo Produkt {i} - Premium",
                description="Hochwertiges Produkt.",
                listing_status="active",
                category_id="12345",
                price_eur=Decimal("29.99"),
                quantity_available=10,
                optimization_status="healthy",
            ))

        db.add(BankTransaction(
            bank_ref="TX-DEMO-1",
            transaction_date=datetime.now(timezone.utc),
            amount=Decimal("29.99"),
            description="eBay Auszahlung",
            counterparty_name="eBay",
            status="pending",
        ))
        db.commit()
        print("Seed: 5 Produkte/Listings + 1 Bankbuchung angelegt.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
