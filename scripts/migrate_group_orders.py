"""Migration: Mehrpositions-Bestellungen (2026-07-05).

1. UNIQUE-Constraint auf orders_aliexpress.aliexpress_order_id entfernen
   (Tabellen-Rebuild) — bei Buendel-Bestellungen teilen sich mehrere Sales
   EINE AliExpress-Order-ID. sale_id bleibt UNIQUE (1:1 Sale<->Order).
2. Index idx_sale_ebay_order auf sales.ebay_order_id anlegen (Gruppierung).

Neue SPALTEN (sales.ebay_cancel_state, orders_aliexpress.source_aliexpress_id)
legt init_db beim App-Start selbst an — hier nicht noetig.

Aufruf (im App-Verzeichnis, App vorher stoppen):
    .venv/bin/python scripts/migrate_group_orders.py [--db data/ebay_store.db]

Legt vorher automatisch ein Backup <db>.bak-migrate-group an.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from pathlib import Path


def migrate(db_path: str) -> None:
    p = Path(db_path)
    if not p.exists():
        sys.exit(f"DB nicht gefunden: {p}")
    backup = p.with_suffix(p.suffix + ".bak-migrate-group")
    shutil.copy2(p, backup)
    print(f"Backup: {backup}")

    conn = sqlite3.connect(str(p))
    try:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='orders_aliexpress'"
        ).fetchone()
        if row is None:
            sys.exit("Tabelle orders_aliexpress fehlt")
        ddl = row[0]

        # UNIQUE auf aliexpress_order_id entfernen (Inline- und Constraint-Variante)
        new_ddl = re.sub(r",\s*UNIQUE\s*\(\s*aliexpress_order_id\s*\)", "", ddl)
        new_ddl = re.sub(r'("?aliexpress_order_id"?\s+VARCHAR\(\d+\))\s+UNIQUE',
                         r"\1", new_ddl)
        if new_ddl == ddl:
            print("Kein UNIQUE(aliexpress_order_id) mehr vorhanden — Schritt 1 übersprungen.")
        else:
            cols = [r[1] for r in cur.execute("PRAGMA table_info(orders_aliexpress)")]
            col_list = ", ".join(f'"{c}"' for c in cols)
            new_ddl = new_ddl.replace("CREATE TABLE orders_aliexpress",
                                      "CREATE TABLE orders_aliexpress_new", 1)
            cur.execute("PRAGMA foreign_keys=OFF")
            cur.execute("BEGIN")
            cur.execute(new_ddl)
            cur.execute(f"INSERT INTO orders_aliexpress_new ({col_list}) "
                        f"SELECT {col_list} FROM orders_aliexpress")
            n_old = cur.execute("SELECT COUNT(*) FROM orders_aliexpress").fetchone()[0]
            n_new = cur.execute("SELECT COUNT(*) FROM orders_aliexpress_new").fetchone()[0]
            if n_old != n_new:
                conn.rollback()
                sys.exit(f"Zeilen-Mismatch ({n_old} vs {n_new}) — Rollback, nichts geändert.")
            cur.execute("DROP TABLE orders_aliexpress")
            cur.execute("ALTER TABLE orders_aliexpress_new RENAME TO orders_aliexpress")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ae_order "
                        "ON orders_aliexpress (aliexpress_order_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ae_sale "
                        "ON orders_aliexpress (sale_id)")
            conn.commit()
            cur.execute("PRAGMA foreign_keys=ON")
            print(f"orders_aliexpress neu aufgebaut ({n_new} Zeilen), UNIQUE entfernt.")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_sale_ebay_order "
                     "ON sales (ebay_order_id)")
        conn.commit()
        print("Index idx_sale_ebay_order ok.")

        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        print(f"integrity_check: {ok}")
        if ok != "ok":
            sys.exit("Integritaetscheck fehlgeschlagen — Backup einspielen!")
    finally:
        conn.close()
    print("Migration fertig.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/ebay_store.db")
    args = ap.parse_args()
    migrate(args.db)
