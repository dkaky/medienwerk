"""Create a sanitized, read-only business snapshot from the production SQLite DB.

This module deliberately does not import application settings, ORM models, services,
integrations, or scheduler code.  It opens an explicitly supplied SQLite file with
``mode=ro`` and exports only the reviewed allowlists below.

The ZIP is suitable for a private, short-lived GitHub Actions artifact.  Exported row
contents are never logged; the CLI writes only generic status information to stderr.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sqlite3
import sys
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence


EXPORT_VERSION = "1.1.0"
SCHEMA_VERSION = 2
MAX_ROWS_PER_TABLE = 2_000_000
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
ARCHIVE_ROOT = "growth_snapshot"

CSV_FILES = (
    "sales.csv",
    "listings.csv",
    "supplier_orders.csv",
    "product_ideas.csv",
    "invoices_summary.csv",
    "fees.csv",
    "bank_summary.csv",
)
EXPECTED_ARCHIVE_FILES = tuple(
    f"{ARCHIVE_ROOT}/{name}" for name in (*CSV_FILES, "metadata.json")
)

OUTPUT_HEADERS: dict[str, tuple[str, ...]] = {
    "sales.csv": (
        "sale_id", "order_group_id", "listing_id", "has_supplier_order",
        "sale_date", "quantity", "line_total_eur", "status",
        "ebay_cancel_state", "cancel_reviewed", "ae_paid", "actual_fee_present",
    ),
    "listings.csv": (
        "listing_id", "product_id", "ebay_item_id", "ebay_sku", "title",
        "category_id", "category_name", "listing_status", "listing_start_date",
        "created_date", "price_eur", "cost_eur", "supplier_shipping_eur",
        "ad_rate_pct", "quantity_available", "supplier_in_stock", "monitor_status",
        "sales_hold", "sales_total_lifetime", "views_30d", "impressions_window",
        "clicks_window", "stats_synced_date", "last_monitored_date",
        "optimization_status", "last_optimization_date", "optimized_date",
        "has_optimization_snapshot", "variant_count", "alternative_supplier_count",
        "aliexpress_product_id", "supplier_id", "supplier_rating",
        "ebay_price_synced_date", "has_live_price_snapshot",
    ),
    "supplier_orders.csv": (
        "supplier_order_id", "supplier_order_group_id", "sale_id", "product_id",
        "source_aliexpress_product_id", "order_date", "quantity", "purchase_cost_eur",
        "cost_source", "import_source", "status", "has_tracking",
        "estimated_delivery_date", "delivery_checked_date", "has_original_receipt",
        "has_generated_invoice",
    ),
    "product_ideas.csv": (
        "idea_id", "aliexpress_product_id", "status", "created_date", "updated_date",
        "source", "from_trend", "category", "niche", "title", "cost_eur",
        "price_eur", "profit_eur", "margin_pct", "rating", "reviews",
        "orders_volume", "delivery_days", "store_id", "store_name", "is_choice",
        "ships_from", "alternative_count", "has_import_error",
    ),
    "invoices_summary.csv": (
        "month", "invoice_type", "category", "currency", "document_count",
        "amount_total", "linked_sale_count", "linked_supplier_order_count",
        "original_document_count", "generated_document_count",
        "unlinked_document_count",
    ),
    "fees.csv": (
        "sale_id", "listing_id", "sale_date", "sale_status", "line_total_eur",
        "actual_fee_eur", "actual_fee_present", "actual_fee_is_zero",
        "actual_fee_pct_of_gross",
    ),
    "bank_summary.csv": (
        "month", "direction", "match_status", "accounting_category",
        "transaction_count", "amount_total_eur", "linked_sale_count",
        "linked_supplier_order_count", "with_invoice_count",
    ),
}

# Columns that queries are allowed to depend on.  Missing columns are fatal.
REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "products": frozenset({
        "id", "aliexpress_id", "variants", "alternatives", "supplier_id",
        "supplier_rating",
    }),
    "listings": frozenset({
        "id", "product_id", "ebay_item_id", "ebay_sku", "title_seo", "category_id",
        "category_name", "listing_status", "listing_start_date", "created_at",
        "updated_at",
        "price_eur", "cost_eur", "supplier_ship_eur", "ad_rate_pct",
        "quantity_available", "supplier_in_stock", "monitor_status", "sales_hold",
        "sales_total", "views_30d", "impressions_week", "clicks_week",
        "stats_synced_at", "last_monitored_at", "optimization_status",
        "last_optimization_date", "optimized_at", "opt_snapshot",
        "ebay_price_synced_at", "ebay_live_prices",
    }),
    "price_history": frozenset({"id", "listing_id", "created_at"}),
    "sales": frozenset({
        "id", "ebay_order_id", "listing_id", "sale_date", "created_at", "quantity",
        "price_eur", "status", "ebay_cancel_state", "cancel_reviewed", "ae_paid",
        "fee_eur_actual",
    }),
    "orders_aliexpress": frozenset({
        "id", "sale_id", "aliexpress_order_id", "product_id", "source_aliexpress_id",
        "order_date", "created_at", "quantity", "cost_cny", "cost_source",
        "invoice_data", "status", "tracking_number", "estimated_delivery",
        "delivery_checked_at",
    }),
    "invoices": frozenset({
        "id", "type", "category", "currency", "amount", "sale_id", "order_id",
        "is_original", "generated_path", "invoice_date", "created_at",
    }),
    "bank_transactions": frozenset({
        "id", "transaction_date", "amount", "status", "sale_id", "order_id",
        "invoices", "kontierung",
    }),
    "product_ideas": frozenset({
        "id", "aliexpress_id", "status", "created_at", "updated_at", "source",
        "from_trend", "category", "niche", "title", "cost_eur", "price_eur",
        "profit_eur", "margin_pct", "rating", "reviews", "orders_volume",
        "delivery_days", "store_id", "store_name", "is_choice", "ships_from",
        "alternatives", "import_error",
    }),
    "task_logs": frozenset({"id", "task_type", "status", "created_at", "updated_at"}),
}

# Complete reviewed column inventory.  Known sensitive columns are intentionally present
# here so a normal production schema passes validation, but no query selects their values.
KNOWN_COLUMNS: dict[str, frozenset[str]] = {
    "products": frozenset({
        "id", "aliexpress_url", "aliexpress_id", "title_raw", "description_raw",
        "price_cny", "images", "variants", "supplier_id", "supplier_rating",
        "created_at", "updated_at", "alternatives",
    }),
    "listings": frozenset({
        "id", "product_id", "ebay_item_id", "ebay_sku", "image_url", "title_seo",
        "description", "category_id", "category_name", "listing_status", "price_eur",
        "quantity_available", "impressions_week", "clicks_week", "optimization_status",
        "optimization_stage", "last_optimization_date", "optimized_at", "opt_snapshot",
        "listing_start_date", "cleanup_dismissed_at", "opt_dismissed_at",
        "volume_promotion_id", "optimization_suggestion", "ebay_draft_id",
        "publish_queued", "publish_error", "variant_map", "variant_prices",
        "variant_stock", "variant_source_map", "self_stock", "cost_eur",
        "supplier_ship_eur", "markup_pct", "auto_reprice", "min_price_eur",
        "max_price_eur", "min_profit_eur", "ebay_variant_images", "image_audit_at",
        "ebay_gallery",
        "ebay_live_prices", "ebay_price_synced_at", "supplier_in_stock",
        "monitor_status", "sales_hold", "hold_reason", "last_monitored_at",
        "item_specifics", "sales_total", "views_30d", "stats_synced_at",
        "ad_rate_pct", "shipping_policy_id", "shipping_policy_name", "created_at",
        "updated_at",
    }),
    "price_history": frozenset({
        "id", "listing_id", "old_price_eur", "new_price_eur", "cost_eur",
        "profit_eur", "margin_pct", "reason", "created_at", "updated_at",
    }),
    "sales": frozenset({
        "id", "ebay_transaction_id", "ebay_order_id", "ebay_line_item_id",
        "listing_id", "buyer_name", "buyer_email", "delivery_address", "quantity",
        "variant_selected", "price_eur", "fee_eur_actual", "sale_date", "status",
        "ebay_cancel_state", "cancel_reviewed", "ae_paid", "delivery_check",
        "created_at", "updated_at",
    }),
    "orders_aliexpress": frozenset({
        "id", "sale_id", "aliexpress_order_id", "product_id", "source_aliexpress_id",
        "variant_selected", "quantity", "cost_cny", "cost_source", "delivery_name",
        "delivery_address", "order_date", "status", "tracking_number",
        "tracking_carrier", "estimated_delivery", "delivery_checked_at", "invoice_data",
        "created_at", "updated_at",
    }),
    "invoices": frozenset({
        "id", "type", "category", "reference_id", "note", "invoice_number",
        "invoice_date", "amount", "currency", "file_path", "file_hash", "is_original",
        "generated_path", "generated_hash", "receipt_data", "sale_id", "order_id",
        "created_at",
    }),
    "bank_transactions": frozenset({
        "id", "bank_ref", "transaction_date", "amount", "description",
        "counterparty_name", "status", "sale_id", "order_id", "invoices", "match_info",
        "kontierung", "kontierung_source", "created_at", "updated_at",
    }),
    "product_ideas": frozenset({
        "id", "aliexpress_id", "aliexpress_url", "title", "short_desc", "image_url",
        "category", "niche", "cost_eur", "price_eur", "profit_eur", "margin_pct",
        "rating", "reviews", "orders_volume", "delivery_days", "store_name", "store_id",
        "is_choice", "alternatives", "from_trend", "ships_from", "source", "source_note",
        "source_url", "status", "import_error", "publish_requested", "created_at",
        "updated_at",
    }),
    "task_logs": frozenset({
        "id", "task_type", "reference_id", "status", "error_message", "result_data",
        "retry_count", "created_at", "updated_at",
    }),
}

KNOWN_SENSITIVE_COLUMNS: dict[str, frozenset[str]] = {
    "sales": frozenset({
        "buyer_name", "buyer_email", "delivery_address", "delivery_check",
        "ebay_transaction_id", "ebay_line_item_id", "variant_selected",
    }),
    "orders_aliexpress": frozenset({
        "delivery_name", "delivery_address", "tracking_number", "variant_selected",
        "invoice_data", "aliexpress_order_id",
    }),
    "invoices": frozenset({
        "reference_id", "note", "invoice_number", "file_path", "file_hash",
        "generated_path", "generated_hash", "receipt_data",
    }),
    "bank_transactions": frozenset({
        "bank_ref", "description", "counterparty_name", "match_info", "invoices",
    }),
    "task_logs": frozenset({"reference_id", "error_message", "result_data"}),
    "product_ideas": frozenset({"short_desc", "source_note", "import_error"}),
    "listings": frozenset({
        "description", "publish_error", "hold_reason", "optimization_suggestion",
    }),
    "products": frozenset({"description_raw"}),
}

UNSAFE_COLUMN_PATTERN = re.compile(
    r"(?:buyer|customer|e_?mail|phone|mobile|address|street|password|passwd|secret|"
    r"token|oauth|credential|api_?key|payment|card|iban|bic|counterparty|tracking|"
    r"delivery_name|receipt|error_message|result_data|note|file_path)",
    re.IGNORECASE,
)
EMAIL_PATTERN = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
PHONE_PATTERN = re.compile(r"(?<!\w)(?:\+?\d[\d\s()./-]{7,}\d)(?!\w)")
DATE_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:[ T](?P<time>\d{2}:\d{2}:\d{2})(?:\.\d{1,6})?"
    r"(?:Z|[+-]\d{2}:\d{2})?)?$"
)
COMMIT_PATTERN = re.compile(r"^(?:unknown|[0-9a-fA-F]{7,40})$")

SALE_STATUSES = frozenset({
    "pending", "ordered_aliexpress", "tracking", "delivered", "refunded",
    "cancelled", "canceled", "storniert", "needs_manual_review",
    "alternative_pending", "manual_intervention_required", "self_shipped",
})
LISTING_STATUSES = frozenset({"active", "draft", "ended", "delist_pending"})
ORDER_STATUSES = frozenset({
    "ordering", "pending", "ordered", "shipped", "delivered", "failed", "imported",
    "verify_needed",
})
IDEA_STATUSES = frozenset({
    "new", "kept", "rejected", "importing", "imported", "import_failed",
})
COST_SOURCES = frozenset({"api", "estimate", "receipt", "manual"})
IMPORT_SOURCES = frozenset({"browser", "csv", "api", "manual", "fulfillment"})

SHIP_FROM_MAP = {
    "de": "DE", "germany": "DE", "deutschland": "DE",
    "pl": "PL", "poland": "PL", "polen": "PL",
    "fr": "FR", "france": "FR", "frankreich": "FR",
    "es": "ES", "spain": "ES", "spanien": "ES",
    "cz": "CZ", "czech republic": "CZ", "czechia": "CZ", "tschechien": "CZ",
    "it": "IT", "italy": "IT", "italien": "IT",
    "be": "BE", "belgium": "BE", "belgien": "BE",
    "nl": "NL", "netherlands": "NL", "niederlande": "NL",
    "pt": "PT", "portugal": "PT", "at": "AT", "austria": "AT", "österreich": "AT",
    "hu": "HU", "hungary": "HU", "sk": "SK", "slovakia": "SK",
    "si": "SI", "slovenia": "SI", "ro": "RO", "romania": "RO",
    "bg": "BG", "bulgaria": "BG", "hr": "HR", "croatia": "HR",
    "gr": "GR", "greece": "GR", "ie": "IE", "ireland": "IE",
    "lu": "LU", "luxembourg": "LU", "lt": "LT", "lithuania": "LT",
    "lv": "LV", "latvia": "LV", "ee": "EE", "estonia": "EE",
    "se": "SE", "sweden": "SE", "fi": "FI", "finland": "FI",
    "dk": "DK", "denmark": "DK", "eu": "EU", "european union": "EU",
    "cn": "CN", "china": "CN", "us": "US", "usa": "US",
    "uk": "GB", "gb": "GB", "united kingdom": "GB",
}


class SnapshotExportError(RuntimeError):
    """Base exception for safe, user-facing export failures."""


class SchemaValidationError(SnapshotExportError):
    """The database schema does not match the reviewed contract."""


class UnsafeSchemaError(SchemaValidationError):
    """An unreviewed column looks sensitive; exporting must stop."""


def _quote_identifier(value: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", value):
        raise SchemaValidationError("invalid internal SQL identifier")
    return '"' + value + '"'


@contextmanager
def _read_only_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    path = db_path.resolve(strict=True)
    if not path.is_file():
        raise SnapshotExportError("source database is not a regular file")
    uri = path.as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise SnapshotExportError("could not open source database read-only") from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        query_only = connection.execute("PRAGMA query_only").fetchone()[0]
        if int(query_only) != 1:
            raise SnapshotExportError("SQLite query-only enforcement is unavailable")
        connection.execute("BEGIN")
        yield connection
    except sqlite3.Error as exc:
        raise SnapshotExportError("read-only database query failed") from exc
    finally:
        connection.close()


def _validate_schema(connection: sqlite3.Connection) -> dict[str, dict[str, int]]:
    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    existing = {str(row[0]) for row in table_rows}
    missing_tables = sorted(set(REQUIRED_COLUMNS) - existing)
    if missing_tables:
        raise SchemaValidationError(
            "required source tables are missing: " + ", ".join(missing_tables)
        )

    coverage: dict[str, dict[str, int]] = {}
    for table, required in REQUIRED_COLUMNS.items():
        pragma = f"PRAGMA table_info({_quote_identifier(table)})"
        actual = {str(row[1]) for row in connection.execute(pragma).fetchall()}
        missing = sorted(required - actual)
        if missing:
            raise SchemaValidationError(
                f"required columns are missing from {table}: " + ", ".join(missing)
            )
        unexpected = sorted(actual - KNOWN_COLUMNS[table])
        if unexpected:
            if any(UNSAFE_COLUMN_PATTERN.search(column) for column in unexpected):
                raise UnsafeSchemaError(
                    f"unreviewed sensitive-looking columns exist in {table}"
                )
            raise SchemaValidationError(f"unreviewed columns exist in {table}")

        count = int(connection.execute(
            f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
        ).fetchone()[0])
        if count > MAX_ROWS_PER_TABLE:
            raise SnapshotExportError(f"source table {table} exceeds the reviewed row limit")
        coverage[table] = {
            "rows": count,
            "required_columns_checked": len(required),
            "known_sensitive_columns_present": len(
                actual & KNOWN_SENSITIVE_COLUMNS.get(table, frozenset())
            ),
        }
    return coverage


def _bool(value: object) -> str:
    return "true" if bool(value) else "false"


def _date(value: object) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    match = DATE_PATTERN.fullmatch(text)
    if not match:
        raise SnapshotExportError("a source date has an unexpected format")
    try:
        if match.group("time"):
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            datetime.strptime(match.group("date"), "%Y-%m-%d")
    except ValueError as exc:
        raise SnapshotExportError("a source date is invalid") from exc
    return match.group("date")


def _metadata_date(value: object) -> str | None:
    if value is None:
        return None
    normalized = _date(value)
    if not normalized:
        raise SnapshotExportError("a metadata source date has an unexpected format")
    return normalized


def _money(value: object) -> str:
    if value in (None, ""):
        return ""
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise SnapshotExportError("a monetary source value is invalid") from exc
    return format(amount, ".2f")


def _number(value: object, *, places: int = 6) -> str:
    if value in (None, ""):
        return ""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SnapshotExportError("a numeric source value is invalid") from exc
    text = format(number.quantize(Decimal(1).scaleb(-places)), "f")
    return text.rstrip("0").rstrip(".") or "0"


def _integer(value: object) -> str:
    if value in (None, ""):
        return ""
    try:
        return str(int(value))
    except (TypeError, ValueError) as exc:
        raise SnapshotExportError("an integer source value is invalid") from exc


def _safe_text(value: object, *, max_length: int = 500) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x00", "").replace("\r", " ").replace("\n", " ").strip()
    text = EMAIL_PATTERN.sub("[redacted-email]", text)
    text = PHONE_PATTERN.sub("[redacted-phone]", text)
    text = text[:max_length]
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        text = "'" + text
    return text


def _safe_identifier(value: object, *, max_length: int) -> str:
    """Sanitize a reviewed public/business identifier without treating digits as a phone."""
    if value is None:
        return ""
    text = str(value).replace("\x00", "").replace("\r", "").replace("\n", "").strip()
    text = text[:max_length]
    if text.startswith(("=", "+", "-", "@", "\t")):
        text = "'" + text
    return text


def _safe_enum(value: object, allowed: frozenset[str]) -> str:
    if value in (None, ""):
        return ""
    normalized = str(value).strip().lower()
    return normalized if normalized in allowed else "other"


def _safe_structured_label(value: object, *, max_length: int = 60) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if not re.fullmatch(r"[A-Za-z0-9 _./:+-]{1," + str(max_length) + r"}", text):
        return "other"
    return _safe_text(text, max_length=max_length)


def _json_value(value: object) -> object:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _nested_count(value: object, keys: Sequence[str]) -> int:
    parsed = _json_value(value)
    if isinstance(parsed, list):
        return len(parsed)
    if isinstance(parsed, dict):
        for key in keys:
            child = parsed.get(key)
            if isinstance(child, list):
                return len(child)
        return len(parsed) if parsed else 0
    return 0


def _ships_from(value: object) -> str:
    parsed = _json_value(value)
    values = parsed if isinstance(parsed, list) else ([parsed] if isinstance(parsed, str) else [])
    normalized = set()
    for item in values[:50]:
        key = str(item).strip().lower()
        normalized.add(SHIP_FROM_MAP.get(key, "OTHER"))
    return "|".join(sorted(normalized))


def _opaque_groups(values: Iterable[object], prefix: str) -> dict[str, str]:
    unique = sorted({str(value) for value in values if value not in (None, "")})
    return {value: f"{prefix}-{index:06d}" for index, value in enumerate(unique, 1)}


def _validate_output_destination(db_path: Path | str, destination: Path) -> None:
    source = Path(db_path).resolve(strict=True)
    forbidden = {
        source,
        Path(str(source) + "-wal"),
        Path(str(source) + "-shm"),
        Path(str(source) + "-journal"),
    }
    if destination in forbidden:
        raise SnapshotExportError("output destination conflicts with the source database")


def _assert_headers(file_name: str, row: Mapping[str, object]) -> None:
    expected = OUTPUT_HEADERS[file_name]
    if tuple(row.keys()) != expected:
        raise SnapshotExportError(f"internal output allowlist mismatch for {file_name}")


def _csv_bytes(file_name: str, rows: Sequence[Mapping[str, object]]) -> bytes:
    headers = OUTPUT_HEADERS[file_name]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=headers, extrasaction="raise", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        _assert_headers(file_name, row)
        writer.writerow(row)
    return stream.getvalue().encode("utf-8")


def _sales_rows(connection: sqlite3.Connection) -> list[dict[str, object]]:
    raw = connection.execute(
        """
        SELECT s.id AS sale_id,
               s.ebay_order_id AS external_order_id,
               s.listing_id AS listing_id,
               CASE WHEN EXISTS (
                   SELECT 1 FROM orders_aliexpress o WHERE o.sale_id = s.id
               ) THEN 1 ELSE 0 END AS has_supplier_order,
               COALESCE(s.sale_date, s.created_at) AS effective_sale_date,
               s.quantity AS quantity,
               s.price_eur AS line_total_eur,
               s.status AS status,
               s.ebay_cancel_state AS ebay_cancel_state,
               s.cancel_reviewed AS cancel_reviewed,
               s.ae_paid AS ae_paid,
               CASE WHEN s.fee_eur_actual IS NOT NULL THEN 1 ELSE 0 END AS actual_fee_present
        FROM sales s
        ORDER BY s.id
        """
    ).fetchall()
    groups = _opaque_groups((row["external_order_id"] for row in raw), "ebay-order")
    result = []
    for row in raw:
        quantity = int(row["quantity"] or 1)
        result.append(dict(zip(OUTPUT_HEADERS["sales.csv"], (
            _integer(row["sale_id"]),
            groups.get(str(row["external_order_id"]), "") if row["external_order_id"] else "",
            _integer(row["listing_id"]),
            _bool(row["has_supplier_order"]),
            _date(row["effective_sale_date"]),
            str(quantity),
            _money(row["line_total_eur"]),
            _safe_enum(row["status"], SALE_STATUSES),
            _safe_structured_label(row["ebay_cancel_state"], max_length=20),
            _bool(row["cancel_reviewed"]),
            _bool(row["ae_paid"]),
            _bool(row["actual_fee_present"]),
        ))))
    return result


def _listing_rows(connection: sqlite3.Connection) -> list[dict[str, object]]:
    raw = connection.execute(
        """
        SELECT l.id AS listing_id, l.product_id, l.ebay_item_id, l.ebay_sku,
               l.title_seo, l.category_id, l.category_name, l.listing_status,
               l.listing_start_date, l.created_at, l.price_eur, l.cost_eur,
               l.supplier_ship_eur, l.ad_rate_pct, l.quantity_available,
               l.supplier_in_stock, l.monitor_status, l.sales_hold, l.sales_total,
               l.views_30d, l.impressions_week, l.clicks_week, l.stats_synced_at,
               l.last_monitored_at, l.optimization_status, l.last_optimization_date,
               l.optimized_at,
               CASE WHEN l.opt_snapshot IS NOT NULL
                          AND TRIM(CAST(l.opt_snapshot AS TEXT)) NOT IN ('', '{}', 'null')
                    THEN 1 ELSE 0 END AS has_optimization_snapshot,
               p.variants AS product_variants, p.alternatives AS product_alternatives,
               p.aliexpress_id, p.supplier_id, p.supplier_rating,
               l.ebay_price_synced_at,
               CASE WHEN l.ebay_live_prices IS NOT NULL
                          AND TRIM(CAST(l.ebay_live_prices AS TEXT)) NOT IN ('', '{}', 'null')
                    THEN 1 ELSE 0 END AS has_live_price_snapshot
        FROM listings l
        LEFT JOIN products p ON p.id = l.product_id
        ORDER BY l.id
        """
    ).fetchall()
    result = []
    for row in raw:
        result.append(dict(zip(OUTPUT_HEADERS["listings.csv"], (
            _integer(row["listing_id"]), _integer(row["product_id"]),
            _safe_identifier(row["ebay_item_id"], max_length=30),
            _safe_identifier(row["ebay_sku"], max_length=100),
            _safe_text(row["title_seo"], max_length=500),
            _safe_identifier(row["category_id"], max_length=50),
            _safe_text(row["category_name"], max_length=255),
            _safe_enum(row["listing_status"], LISTING_STATUSES),
            _date(row["listing_start_date"]), _date(row["created_at"]),
            _money(row["price_eur"]), _money(row["cost_eur"]),
            _money(row["supplier_ship_eur"]), _number(row["ad_rate_pct"]),
            _integer(row["quantity_available"]), _bool(row["supplier_in_stock"]),
            _safe_structured_label(row["monitor_status"], max_length=30),
            _bool(row["sales_hold"]), _integer(row["sales_total"]),
            _integer(row["views_30d"]), _integer(row["impressions_week"]),
            _integer(row["clicks_week"]), _date(row["stats_synced_at"]),
            _date(row["last_monitored_at"]),
            _safe_structured_label(row["optimization_status"], max_length=30),
            _date(row["last_optimization_date"]), _date(row["optimized_at"]),
            _bool(row["has_optimization_snapshot"]),
            str(_nested_count(row["product_variants"], ("skus", "variants", "items"))),
            str(_nested_count(row["product_alternatives"], ("sources", "items"))),
            _safe_identifier(row["aliexpress_id"], max_length=50),
            _safe_identifier(row["supplier_id"], max_length=100),
            _number(row["supplier_rating"]), _date(row["ebay_price_synced_at"]),
            _bool(row["has_live_price_snapshot"]),
        ))))
    return result


def _supplier_order_rows(connection: sqlite3.Connection) -> list[dict[str, object]]:
    raw = connection.execute(
        """
        SELECT o.id AS supplier_order_id, o.aliexpress_order_id AS external_order_id,
               o.sale_id, o.product_id, o.source_aliexpress_id,
               COALESCE(o.order_date, o.created_at) AS effective_order_date,
               o.quantity, o.cost_cny, o.cost_source,
               CASE WHEN json_valid(o.invoice_data)
                    THEN json_extract(o.invoice_data, '$.source') ELSE NULL END AS import_source,
               o.status,
               CASE WHEN o.tracking_number IS NOT NULL AND TRIM(o.tracking_number) <> ''
                    THEN 1 ELSE 0 END AS has_tracking,
               o.estimated_delivery, o.delivery_checked_at,
               CASE WHEN EXISTS (
                   SELECT 1 FROM invoices i
                   WHERE i.order_id = o.id AND COALESCE(i.is_original, 0) = 1
               ) THEN 1 ELSE 0 END AS has_original_receipt,
               CASE WHEN EXISTS (
                   SELECT 1 FROM invoices i
                   WHERE i.order_id = o.id AND i.generated_path IS NOT NULL
                         AND TRIM(i.generated_path) <> ''
               ) THEN 1 ELSE 0 END AS has_generated_invoice
        FROM orders_aliexpress o
        ORDER BY o.id
        """
    ).fetchall()
    groups = _opaque_groups((row["external_order_id"] for row in raw), "supplier-order")
    result = []
    for row in raw:
        result.append(dict(zip(OUTPUT_HEADERS["supplier_orders.csv"], (
            _integer(row["supplier_order_id"]),
            groups.get(str(row["external_order_id"]), "") if row["external_order_id"] else "",
            _integer(row["sale_id"]), _integer(row["product_id"]),
            _safe_identifier(row["source_aliexpress_id"], max_length=50),
            _date(row["effective_order_date"]), _integer(row["quantity"]),
            _money(row["cost_cny"]), _safe_enum(row["cost_source"], COST_SOURCES),
            _safe_enum(row["import_source"], IMPORT_SOURCES),
            _safe_enum(row["status"], ORDER_STATUSES), _bool(row["has_tracking"]),
            _date(row["estimated_delivery"]), _date(row["delivery_checked_at"]),
            _bool(row["has_original_receipt"]), _bool(row["has_generated_invoice"]),
        ))))
    return result


def _product_idea_rows(connection: sqlite3.Connection) -> list[dict[str, object]]:
    raw = connection.execute(
        """
        SELECT id, aliexpress_id, status, created_at, updated_at, source, from_trend,
               category, niche, title, cost_eur, price_eur, profit_eur, margin_pct,
               rating, reviews, orders_volume, delivery_days, store_id, store_name,
               is_choice, ships_from, alternatives,
               CASE WHEN import_error IS NOT NULL AND TRIM(import_error) <> ''
                    THEN 1 ELSE 0 END AS has_import_error
        FROM product_ideas
        ORDER BY id
        """
    ).fetchall()
    result = []
    for row in raw:
        result.append(dict(zip(OUTPUT_HEADERS["product_ideas.csv"], (
            _integer(row["id"]), _safe_identifier(row["aliexpress_id"], max_length=40),
            _safe_enum(row["status"], IDEA_STATUSES), _date(row["created_at"]),
            _date(row["updated_at"]), _safe_structured_label(row["source"], max_length=20),
            _bool(row["from_trend"]), _safe_text(row["category"], max_length=255),
            _safe_text(row["niche"], max_length=120), _safe_text(row["title"], max_length=500),
            _money(row["cost_eur"]), _money(row["price_eur"]), _money(row["profit_eur"]),
            _number(row["margin_pct"]), _number(row["rating"]), _integer(row["reviews"]),
            _integer(row["orders_volume"]), _integer(row["delivery_days"]),
            _safe_identifier(row["store_id"], max_length=32),
            _safe_text(row["store_name"], max_length=255), _bool(row["is_choice"]),
            _ships_from(row["ships_from"]),
            str(_nested_count(row["alternatives"], ("sources", "items"))),
            _bool(row["has_import_error"]),
        ))))
    return result


def _invoice_summary_rows(connection: sqlite3.Connection) -> list[dict[str, object]]:
    raw = connection.execute(
        """
        WITH invoice_base AS (
            SELECT i.id, i.type, i.category, i.currency, i.amount,
                   CASE WHEN s.id IS NOT NULL THEN 1 ELSE 0 END AS valid_sale,
                   CASE WHEN o.id IS NOT NULL THEN 1 ELSE 0 END AS valid_order,
                   COALESCE(i.is_original, 0) AS is_original,
                   CASE WHEN i.generated_path IS NOT NULL AND TRIM(i.generated_path) <> ''
                        THEN 1 ELSE 0 END AS is_generated,
                   COALESCE(substr(s.sale_date, 1, 7), substr(o.order_date, 1, 7),
                            substr(i.invoice_date, 1, 7), substr(i.created_at, 1, 7), '') AS month
            FROM invoices i
            LEFT JOIN sales s ON s.id = i.sale_id
            LEFT JOIN orders_aliexpress o ON o.id = i.order_id
        )
        SELECT month, type AS invoice_type, category, currency,
               COUNT(*) AS document_count,
               ROUND(COALESCE(SUM(amount), 0), 2) AS amount_total,
               SUM(valid_sale) AS linked_sale_count,
               SUM(valid_order) AS linked_supplier_order_count,
               SUM(is_original) AS original_document_count,
               SUM(is_generated) AS generated_document_count,
               SUM(CASE WHEN valid_sale = 0 AND valid_order = 0 THEN 1 ELSE 0 END)
                   AS unlinked_document_count
        FROM invoice_base
        GROUP BY month, type, category, currency
        ORDER BY month, type, category, currency
        """
    ).fetchall()
    result = []
    for row in raw:
        result.append(dict(zip(OUTPUT_HEADERS["invoices_summary.csv"], (
            _safe_structured_label(row["month"], max_length=7),
            _safe_structured_label(row["invoice_type"], max_length=30),
            _safe_text(row["category"], max_length=60),
            _safe_structured_label(row["currency"], max_length=3),
            _integer(row["document_count"]), _money(row["amount_total"]),
            _integer(row["linked_sale_count"]),
            _integer(row["linked_supplier_order_count"]),
            _integer(row["original_document_count"]),
            _integer(row["generated_document_count"]),
            _integer(row["unlinked_document_count"]),
        ))))
    return result


def _fee_rows(connection: sqlite3.Connection) -> list[dict[str, object]]:
    raw = connection.execute(
        """
        SELECT id AS sale_id, listing_id, COALESCE(sale_date, created_at) AS effective_sale_date,
               status, quantity, price_eur, fee_eur_actual
        FROM sales
        ORDER BY id
        """
    ).fetchall()
    result = []
    for row in raw:
        line_total = Decimal(str(row["price_eur"] or 0))
        fee = None if row["fee_eur_actual"] is None else Decimal(str(row["fee_eur_actual"]))
        fee_pct = (
            fee / line_total * Decimal("100")
            if fee is not None and line_total > 0
            else None
        )
        result.append(dict(zip(OUTPUT_HEADERS["fees.csv"], (
            _integer(row["sale_id"]), _integer(row["listing_id"]),
            _date(row["effective_sale_date"]), _safe_enum(row["status"], SALE_STATUSES),
            _money(line_total), _money(fee), _bool(fee is not None),
            _bool(fee == 0 if fee is not None else False),
            _number(fee_pct),
        ))))
    return result


def _bank_summary_rows(connection: sqlite3.Connection) -> list[dict[str, object]]:
    raw = connection.execute(
        """
        SELECT COALESCE(substr(transaction_date, 1, 7), '') AS month,
               CASE WHEN amount > 0 THEN 'credit'
                    WHEN amount < 0 THEN 'debit' ELSE 'zero' END AS direction,
               status AS match_status, kontierung AS accounting_category,
               COUNT(*) AS transaction_count,
               ROUND(COALESCE(SUM(amount), 0), 2) AS amount_total_eur,
               SUM(CASE WHEN sale_id IS NOT NULL THEN 1 ELSE 0 END) AS linked_sale_count,
               SUM(CASE WHEN order_id IS NOT NULL THEN 1 ELSE 0 END)
                   AS linked_supplier_order_count,
               SUM(CASE WHEN invoices IS NOT NULL
                             AND TRIM(CAST(invoices AS TEXT)) NOT IN ('', '[]', '{}', 'null')
                        THEN 1 ELSE 0 END) AS with_invoice_count
        FROM bank_transactions
        GROUP BY month, direction, status, kontierung
        ORDER BY month, direction, status, kontierung
        """
    ).fetchall()
    result = []
    for row in raw:
        result.append(dict(zip(OUTPUT_HEADERS["bank_summary.csv"], (
            _safe_structured_label(row["month"], max_length=7),
            _safe_structured_label(row["direction"], max_length=6),
            _safe_structured_label(row["match_status"], max_length=20),
            _safe_text(row["accounting_category"], max_length=40),
            _integer(row["transaction_count"]), _money(row["amount_total_eur"]),
            _integer(row["linked_sale_count"]),
            _integer(row["linked_supplier_order_count"]),
            _integer(row["with_invoice_count"]),
        ))))
    return result


def _date_range(connection: sqlite3.Connection, table: str, expression: str) -> dict[str, object]:
    # Expressions are fixed literals from _metadata; never user input.
    row = connection.execute(
        f"SELECT COUNT(*) AS total, COUNT({expression}) AS populated, "
        f"MIN({expression}) AS earliest, MAX({expression}) AS latest "
        f"FROM {_quote_identifier(table)}"
    ).fetchone()
    earliest = _metadata_date(row["earliest"])
    latest = _metadata_date(row["latest"])
    return {
        "total": int(row["total"]),
        "populated": int(row["populated"]),
        "earliest": earliest,
        "latest": latest,
    }


def _count_metrics(connection: sqlite3.Connection) -> dict[str, dict[str, int | float]]:
    def metric(query: str) -> dict[str, int | float]:
        row = connection.execute(query).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}

    metrics = {
        "sale_to_listing": metric(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN l.id IS NOT NULL THEN 1 ELSE 0 END) AS covered
               FROM sales s LEFT JOIN listings l ON l.id = s.listing_id"""
        ),
        "sale_to_supplier_order": metric(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN EXISTS (
                          SELECT 1 FROM orders_aliexpress o WHERE o.sale_id = s.id
                      ) THEN 1 ELSE 0 END) AS covered
               FROM sales s"""
        ),
        "sale_actual_fee": metric(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN fee_eur_actual IS NOT NULL THEN 1 ELSE 0 END) AS covered,
                      SUM(CASE WHEN fee_eur_actual > 0 THEN 1 ELSE 0 END) AS positive
               FROM sales"""
        ),
        "listing_item_id": metric(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN ebay_item_id IS NOT NULL AND TRIM(ebay_item_id) <> ''
                               THEN 1 ELSE 0 END) AS covered FROM listings"""
        ),
        "listing_start_date": metric(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN listing_start_date IS NOT NULL THEN 1 ELSE 0 END) AS covered
               FROM listings"""
        ),
        "listing_stats": metric(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN stats_synced_at IS NOT NULL THEN 1 ELSE 0 END) AS covered
               FROM listings"""
        ),
        "supplier_order_cost": metric(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN cost_cny IS NOT NULL AND cost_cny > 0 THEN 1 ELSE 0 END)
                          AS covered,
                      SUM(CASE WHEN cost_source IN ('api','receipt','manual') THEN 1 ELSE 0 END)
                          AS provenance_confirmed
               FROM orders_aliexpress"""
        ),
        "supplier_order_tracking": metric(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN tracking_number IS NOT NULL AND TRIM(tracking_number) <> ''
                               THEN 1 ELSE 0 END) AS covered
               FROM orders_aliexpress"""
        ),
        "invoice_linkage": metric(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN s.id IS NOT NULL OR o.id IS NOT NULL THEN 1 ELSE 0 END)
                          AS covered,
                      SUM(CASE WHEN COALESCE(i.is_original,0) = 1 THEN 1 ELSE 0 END)
                          AS original
               FROM invoices i
               LEFT JOIN sales s ON s.id = i.sale_id
               LEFT JOIN orders_aliexpress o ON o.id = i.order_id"""
        ),
        "bank_linkage": metric(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN s.id IS NOT NULL OR o.id IS NOT NULL THEN 1 ELSE 0 END)
                          AS covered
               FROM bank_transactions b
               LEFT JOIN sales s ON s.id = b.sale_id
               LEFT JOIN orders_aliexpress o ON o.id = b.order_id"""
        ),
    }
    for values in metrics.values():
        total = int(values.get("total", 0))
        covered = int(values.get("covered", 0))
        values["coverage_pct"] = round((100.0 * covered / total), 2) if total else 0.0
    return metrics


def _metadata(
    connection: sqlite3.Connection,
    source_coverage: dict[str, dict[str, int]],
    files: dict[str, dict[str, object]],
    generated_at: datetime,
    snapshot_id: str,
    generator_commit: str,
) -> dict[str, object]:
    ranges = {
        "sales": _date_range(connection, "sales", "sale_date"),
        "supplier_orders": _date_range(connection, "orders_aliexpress", "order_date"),
        "listings_start": _date_range(connection, "listings", "listing_start_date"),
        "listings_updated": _date_range(connection, "listings", "updated_at"),
        "listing_stats": _date_range(connection, "listings", "stats_synced_at"),
        "product_ideas": _date_range(connection, "product_ideas", "created_at"),
        "invoices": _date_range(connection, "invoices", "invoice_date"),
        "bank_transactions": _date_range(connection, "bank_transactions", "transaction_date"),
        "price_history": _date_range(connection, "price_history", "created_at"),
        "task_logs": _date_range(connection, "task_logs", "created_at"),
    }
    coverage = _count_metrics(connection)
    warnings: list[str] = []
    for table, info in source_coverage.items():
        if info["rows"] == 0:
            warnings.append(f"empty_table:{table}")
    if coverage["sale_to_supplier_order"]["coverage_pct"] < 100:
        warnings.append("incomplete_sale_to_supplier_order_linkage")
    if coverage["listing_start_date"]["coverage_pct"] < 100:
        warnings.append("incomplete_listing_start_dates")
    if coverage["supplier_order_cost"].get("provenance_confirmed", 0) < coverage[
        "supplier_order_cost"
    ].get("covered", 0):
        warnings.append("supplier_costs_without_confirmed_provenance")
    if coverage["bank_linkage"]["coverage_pct"] < 100:
        warnings.append("incomplete_bank_linkage")

    return {
        "schema_version": SCHEMA_VERSION,
        "export_version": EXPORT_VERSION,
        "snapshot_id": snapshot_id,
        "generated_at_utc": generated_at.astimezone(timezone.utc).isoformat(),
        "generator_git_commit": generator_commit.lower(),
        "source_kind": "production_sqlite_read_only",
        "read_mode": {
            "sqlite_uri_mode": "ro",
            "query_only": True,
            "single_read_transaction": True,
            "immutable": False,
        },
        "files": files,
        "table_row_counts": {table: info["rows"] for table, info in source_coverage.items()},
        "source_table_coverage": source_coverage,
        "business_date_ranges": ranges,
        "freshness": {name: values["latest"] for name, values in ranges.items()},
        "coverage": coverage,
        "currency_semantics": {
            "orders_aliexpress.cost_cny": "exported as purchase_cost_eur; implementation treats the historical field as EUR",
        },
        "field_semantics": {
            "sales.csv.line_total_eur": (
                "Stored Sale.price_eur: the complete eBay line-item total including "
                "allocated shipping; quantity is separate and is not multiplied."
            ),
            "fees.csv.line_total_eur": (
                "The same stored Sale.price_eur line-item total used by sales.csv."
            ),
            "fees.csv.actual_fee_pct_of_gross": (
                "actual_fee_eur divided by line_total_eur, multiplied by 100."
            ),
        },
        "warnings": sorted(set(warnings)),
        "privacy": {
            "contains_customer_pii": False,
            "contains_raw_tracking_numbers": False,
            "contains_raw_invoice_documents": False,
            "contains_secrets": False,
            "external_order_ids": "replaced with snapshot-local opaque group IDs",
        },
    }


def build_snapshot_bytes(
    db_path: Path | str,
    *,
    temp_parent: Path | str | None = None,
    generator_commit: str = "unknown",
    generated_at: datetime | None = None,
) -> bytes:
    """Build and return the sanitized ZIP without modifying the source database."""
    if not COMMIT_PATTERN.fullmatch(generator_commit):
        raise SnapshotExportError("generator commit must be a hexadecimal Git commit or 'unknown'")
    now = generated_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    snapshot_id = str(uuid.uuid4())
    parent = str(Path(temp_parent).resolve()) if temp_parent is not None else None

    with tempfile.TemporaryDirectory(prefix="growth_snapshot_", dir=parent) as temp_dir:
        root = Path(temp_dir) / ARCHIVE_ROOT
        root.mkdir(mode=0o700)
        with _read_only_connection(Path(db_path)) as connection:
            source_coverage = _validate_schema(connection)
            rows_by_file = {
                "sales.csv": _sales_rows(connection),
                "listings.csv": _listing_rows(connection),
                "supplier_orders.csv": _supplier_order_rows(connection),
                "product_ideas.csv": _product_idea_rows(connection),
                "invoices_summary.csv": _invoice_summary_rows(connection),
                "fees.csv": _fee_rows(connection),
                "bank_summary.csv": _bank_summary_rows(connection),
            }

            file_metadata: dict[str, dict[str, object]] = {}
            for file_name in CSV_FILES:
                payload = _csv_bytes(file_name, rows_by_file[file_name])
                (root / file_name).write_bytes(payload)
                file_metadata[file_name] = {
                    "rows": len(rows_by_file[file_name]),
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }

            metadata = _metadata(
                connection, source_coverage, file_metadata, now, snapshot_id, generator_commit
            )
            metadata_payload = (
                json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            (root / "metadata.json").write_bytes(metadata_payload)

        archive_stream = io.BytesIO()
        with zipfile.ZipFile(
            archive_stream, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for member in EXPECTED_ARCHIVE_FILES:
                archive.write(Path(temp_dir) / member, arcname=member)
        payload = archive_stream.getvalue()
        if len(payload) > MAX_ARCHIVE_BYTES:
            raise SnapshotExportError("sanitized snapshot exceeds the reviewed archive size limit")
        with zipfile.ZipFile(io.BytesIO(payload)) as verification:
            if tuple(verification.namelist()) != EXPECTED_ARCHIVE_FILES:
                raise SnapshotExportError("ZIP content does not match the exact export allowlist")
            bad_member = verification.testzip()
            if bad_member is not None:
                raise SnapshotExportError("ZIP integrity validation failed")
        return payload


def write_snapshot(
    db_path: Path | str,
    output_path: Path | str,
    *,
    temp_parent: Path | str | None = None,
    generator_commit: str = "unknown",
    generated_at: datetime | None = None,
) -> Path:
    """Atomically write a sanitized snapshot ZIP to ``output_path``."""
    destination = Path(output_path).resolve()
    _validate_output_destination(db_path, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = build_snapshot_bytes(
        db_path,
        temp_parent=temp_parent,
        generator_commit=generator_commit,
        generated_at=generated_at,
    )
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export a sanitized read-only growth snapshot")
    parser.add_argument("--db", default="data/ebay_store.db", help="SQLite source path")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--output", help="ZIP destination path")
    output.add_argument("--stdout", action="store_true", help="write ZIP bytes to stdout")
    parser.add_argument("--generator-commit", default="unknown")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.stdout:
            payload = build_snapshot_bytes(
                args.db,
                generator_commit=args.generator_commit,
            )
            sys.stdout.buffer.write(payload)
            sys.stdout.buffer.flush()
        else:
            destination = Path(args.output or "growth_snapshot.zip").resolve()
            write_snapshot(
                args.db,
                destination,
                generator_commit=args.generator_commit,
            )
        print("sanitized growth snapshot created successfully", file=sys.stderr)
        return 0
    except SnapshotExportError as exc:
        print(f"sanitized growth snapshot failed: {exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error, ValueError):
        print("sanitized growth snapshot failed safely", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
