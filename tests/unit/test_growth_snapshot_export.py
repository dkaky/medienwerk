"""Security and contract tests for the standalone sanitized growth exporter."""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import re
import sqlite3
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "export_growth_snapshot.py"
WORKFLOW = ROOT / ".github" / "workflows" / "growth-snapshot.yml"
SPEC = importlib.util.spec_from_file_location("export_growth_snapshot", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


INTEGER_COLUMNS = {
    "id", "product_id", "listing_id", "sale_id", "order_id", "quantity",
    "quantity_available", "supplier_in_stock", "sales_hold", "sales_total",
    "views_30d", "impressions_week", "clicks_week", "cancel_reviewed", "ae_paid",
    "is_original", "from_trend", "reviews", "orders_volume", "delivery_days",
    "is_choice",
}
REAL_COLUMNS = {
    "price_eur", "cost_eur", "supplier_ship_eur", "ad_rate_pct", "supplier_rating",
    "fee_eur_actual", "cost_cny", "amount", "profit_eur", "margin_pct", "rating",
}
KNOWN_DENIED_FIXTURE_COLUMNS = {
    "products": {
        "description_raw": "TEXT",
    },
    "listings": {
        "description": "TEXT", "publish_error": "TEXT", "hold_reason": "TEXT",
    },
    "sales": {
        "ebay_transaction_id": "TEXT", "ebay_line_item_id": "TEXT",
        "buyer_name": "TEXT", "buyer_email": "TEXT", "delivery_address": "TEXT",
    },
    "orders_aliexpress": {
        "delivery_name": "TEXT", "delivery_address": "TEXT",
    },
    "invoices": {
        "reference_id": "TEXT", "note": "TEXT", "invoice_number": "TEXT",
        "file_path": "TEXT", "file_hash": "TEXT", "receipt_data": "TEXT",
    },
    "bank_transactions": {
        "bank_ref": "TEXT", "description": "TEXT", "counterparty_name": "TEXT",
        "match_info": "TEXT",
    },
    "product_ideas": {
        "short_desc": "TEXT", "source_note": "TEXT",
    },
    "task_logs": {
        "reference_id": "TEXT", "error_message": "TEXT", "result_data": "TEXT",
    },
}


def _column_type(column: str) -> str:
    if column in INTEGER_COLUMNS:
        return "INTEGER"
    if column in REAL_COLUMNS:
        return "REAL"
    return "TEXT"


def _create_schema(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        for table, required in exporter.REQUIRED_COLUMNS.items():
            columns = {column: _column_type(column) for column in required}
            columns.update(KNOWN_DENIED_FIXTURE_COLUMNS.get(table, {}))
            definition = ", ".join(f'"{name}" {kind}' for name, kind in sorted(columns.items()))
            connection.execute(f'CREATE TABLE "{table}" ({definition})')
        connection.execute("CREATE TABLE app_settings (key TEXT, value TEXT)")
        connection.commit()
    finally:
        connection.close()


def _insert(connection: sqlite3.Connection, table: str, values: dict[str, object]) -> None:
    columns = ", ".join(f'"{column}"' for column in values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', tuple(values.values())
    )


def _populate_fixture(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        _insert(connection, "products", {
            "id": 1,
            "aliexpress_id": "1005000123456789",
            "variants": json.dumps({"skus": [{"id": 1}, {"id": 2}]}),
            "alternatives": json.dumps({"sources": [{"id": 1}]}),
            "supplier_id": "1100123456",
            "supplier_rating": 4.8,
            "description_raw": "PRIVATE_PRODUCT_DESCRIPTION_MARKER",
        })
        _insert(connection, "listings", {
            "id": 10, "product_id": 1, "ebay_item_id": "123456789012",
            "ebay_sku": "SAFE-SKU-1", "title_seo": "=Premium Widget seller@example.test",
            "category_id": "1234", "category_name": "Home", "listing_status": "active",
            "listing_start_date": "2026-01-01 10:00:00", "created_at": "2026-01-01 09:00:00",
            "updated_at": "2026-08-18 08:00:00", "price_eur": 25.0, "cost_eur": 10.0,
            "supplier_ship_eur": 1.99, "ad_rate_pct": 0.12, "quantity_available": 5,
            "supplier_in_stock": 1, "monitor_status": "ok", "sales_hold": 0,
            "sales_total": 3, "views_30d": 20, "impressions_week": 100,
            "clicks_week": 10, "stats_synced_at": "2026-08-18 07:00:00",
            "last_monitored_at": "2026-08-18 08:00:00", "optimization_status": "healthy",
            "last_optimization_date": "2026-08-10 00:00:00",
            "optimized_at": "2026-08-11 00:00:00", "opt_snapshot": '{"sales":2}',
            "ebay_price_synced_at": "2026-08-18 06:00:00",
            "ebay_live_prices": '{"SAFE-SKU-1":25.0}',
            "description": "PRIVATE_LISTING_DESCRIPTION_MARKER",
            "publish_error": "PRIVATE_ERROR_MARKER", "hold_reason": "PRIVATE_HOLD_MARKER",
        })
        _insert(connection, "sales", {
            "id": 100, "ebay_order_id": "RAW_EBAY_ORDER_123456", "listing_id": 10,
            "sale_date": "2026-08-17 12:34:56", "created_at": "2026-08-17 12:35:00",
            "quantity": 2, "price_eur": 25.0, "status": "delivered",
            "ebay_cancel_state": None, "cancel_reviewed": 0, "ae_paid": 1,
            "fee_eur_actual": 7.5, "ebay_transaction_id": "RAW_TX_999",
            "ebay_line_item_id": "RAW_LINE_888", "buyer_name": "PRIVATE_BUYER_NAME",
            "buyer_email": "private-buyer@example.test",
            "delivery_address": json.dumps({
                "street": "PRIVATE_STREET_12", "city": "PRIVATE_CITY",
                "postal": "12345", "phone": "+49 170 1234567",
            }),
        })
        _insert(connection, "orders_aliexpress", {
            "id": 200, "sale_id": 100, "aliexpress_order_id": "RAW_AE_ORDER_654321",
            "product_id": 1, "source_aliexpress_id": "1005000123456789",
            "order_date": "2026-08-17 13:00:00", "created_at": "2026-08-17 13:00:01",
            "quantity": 2, "cost_cny": 19.98, "cost_source": "receipt",
            "invoice_data": json.dumps({"source": "browser", "store": "PRIVATE_STORE_RAW"}),
            "status": "shipped", "tracking_number": "RAW_TRACKING_LP123456789CN",
            "estimated_delivery": "2026-08-25 00:00:00",
            "delivery_checked_at": "2026-08-18 00:00:00",
            "delivery_name": "PRIVATE_DELIVERY_NAME",
            "delivery_address": "PRIVATE_SUPPLIER_ADDRESS",
        })
        _insert(connection, "invoices", {
            "id": 300, "type": "aliexpress_purchase", "category": None,
            "currency": "EUR", "amount": 19.98, "sale_id": 100, "order_id": 200,
            "is_original": 1, "generated_path": "PRIVATE_GENERATED_PATH",
            "invoice_date": "2026-08-17 13:00:00", "created_at": "2026-08-17 13:01:00",
            "reference_id": "PRIVATE_INVOICE_REFERENCE", "note": "PRIVATE_INVOICE_NOTE",
            "invoice_number": "PRIVATE_INVOICE_NUMBER", "file_path": "PRIVATE_FILE_PATH",
            "file_hash": "PRIVATE_FILE_HASH", "receipt_data": "PRIVATE_RECEIPT_DATA",
        })
        _insert(connection, "bank_transactions", {
            "id": 400, "transaction_date": "2026-08-18 01:00:00", "amount": -19.98,
            "status": "matched", "sale_id": None, "order_id": 200,
            "invoices": "[300]", "kontierung": "wareneinkauf",
            "bank_ref": "PRIVATE_BANK_REFERENCE", "description": "PRIVATE_BANK_DESCRIPTION",
            "counterparty_name": "PRIVATE_COUNTERPARTY_NAME",
            "match_info": "PRIVATE_MATCH_INFO",
        })
        _insert(connection, "product_ideas", {
            "id": 500, "aliexpress_id": "1005000999999999", "status": "new",
            "created_at": "2026-08-16 10:00:00", "updated_at": "2026-08-18 10:00:00",
            "source": "aliexpress", "from_trend": 1, "category": "Garden",
            "niche": "widgets", "title": "+Call +49 170 7654321 about idea@example.test",
            "cost_eur": 8.0, "price_eur": 24.0, "profit_eur": 8.5, "margin_pct": 0.354,
            "rating": 4.7, "reviews": 120, "orders_volume": 800, "delivery_days": 4,
            "store_id": "1100999999", "store_name": "@Formula Store", "is_choice": 1,
            "ships_from": json.dumps(["Poland", "Germany", "Unexpected Warehouse Name"]),
            "alternatives": json.dumps([{"id": 1}, {"id": 2}]),
            "import_error": "PRIVATE_IMPORT_ERROR", "short_desc": "PRIVATE_SHORT_DESC",
            "source_note": "PRIVATE_SOURCE_NOTE",
        })
        _insert(connection, "price_history", {
            "id": 600, "listing_id": 10, "created_at": "2026-08-15 00:00:00",
        })
        _insert(connection, "task_logs", {
            "id": 700, "task_type": "monitor", "status": "failed",
            "created_at": "2026-08-18 09:00:00", "updated_at": "2026-08-18 09:00:01",
            "reference_id": "PRIVATE_TASK_REFERENCE", "error_message": "PRIVATE_TASK_ERROR",
            "result_data": "PRIVATE_TASK_RESULT",
        })
        connection.execute(
            "INSERT INTO app_settings(key, value) VALUES (?, ?)",
            ("api_token", "PRIVATE_SECRET_TOKEN_VALUE"),
        )
        connection.commit()
    finally:
        connection.close()


def _database(tmp_path: Path, *, populated: bool = True) -> Path:
    path = tmp_path / "source.db"
    _create_schema(path)
    if populated:
        _populate_fixture(path)
    return path


def _read_archive(path: Path) -> tuple[dict[str, bytes], dict]:
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    metadata = json.loads(members["growth_snapshot/metadata.json"])
    return members, metadata


def _csv_rows(payload: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(payload.decode("utf-8"))))


def test_export_is_read_only_allowlisted_and_sanitized(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    output = tmp_path / "snapshot.zip"
    before_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
    before_mtime = db_path.stat().st_mtime_ns

    exporter.write_snapshot(
        db_path,
        output,
        temp_parent=tmp_path,
        generator_commit="0123456789abcdef0123456789abcdef01234567",
        generated_at=datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc),
    )

    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before_hash
    assert db_path.stat().st_mtime_ns == before_mtime
    members, metadata = _read_archive(output)
    assert tuple(members) == exporter.EXPECTED_ARCHIVE_FILES

    for file_name, expected_headers in exporter.OUTPUT_HEADERS.items():
        rows = _csv_rows(members[f"growth_snapshot/{file_name}"])
        header = next(csv.reader(io.StringIO(
            members[f"growth_snapshot/{file_name}"].decode("utf-8")
        )))
        assert tuple(header) == expected_headers
        for row in rows:
            assert tuple(row) == expected_headers

    all_bytes = b"\n".join(members.values())
    prohibited_values = (
        b"PRIVATE_BUYER_NAME", b"private-buyer@example.test", b"PRIVATE_STREET_12",
        b"PRIVATE_CITY", b"+49 170 1234567", b"RAW_TRACKING_LP123456789CN",
        b"PRIVATE_DELIVERY_NAME", b"PRIVATE_SUPPLIER_ADDRESS", b"RAW_EBAY_ORDER_123456",
        b"RAW_AE_ORDER_654321", b"RAW_TX_999", b"RAW_LINE_888",
        b"PRIVATE_INVOICE_NOTE", b"PRIVATE_RECEIPT_DATA", b"PRIVATE_BANK_DESCRIPTION",
        b"PRIVATE_COUNTERPARTY_NAME", b"PRIVATE_TASK_ERROR", b"PRIVATE_TASK_RESULT",
        b"PRIVATE_SECRET_TOKEN_VALUE", b"PRIVATE_IMPORT_ERROR", b"PRIVATE_SOURCE_NOTE",
    )
    for value in prohibited_values:
        assert value not in all_bytes

    sales = _csv_rows(members["growth_snapshot/sales.csv"])
    assert sales == [{
        "sale_id": "100", "order_group_id": "ebay-order-000001", "listing_id": "10",
        "has_supplier_order": "true", "sale_date": "2026-08-17", "quantity": "2",
        "line_total_eur": "25.00", "status": "delivered",
        "ebay_cancel_state": "", "cancel_reviewed": "false", "ae_paid": "true",
        "actual_fee_present": "true",
    }]
    fees = _csv_rows(members["growth_snapshot/fees.csv"])
    assert fees[0]["line_total_eur"] == "25.00"
    assert fees[0]["actual_fee_pct_of_gross"] == "30"
    supplier_orders = _csv_rows(members["growth_snapshot/supplier_orders.csv"])
    assert supplier_orders[0]["supplier_order_group_id"] == "supplier-order-000001"
    assert supplier_orders[0]["has_tracking"] == "true"
    assert supplier_orders[0]["has_original_receipt"] == "true"
    listings = _csv_rows(members["growth_snapshot/listings.csv"])
    assert listings[0]["ebay_item_id"] == "123456789012"
    assert listings[0]["aliexpress_product_id"] == "1005000123456789"
    assert listings[0]["title"].startswith("'=Premium Widget")
    assert "[redacted-email]" in listings[0]["title"]
    ideas = _csv_rows(members["growth_snapshot/product_ideas.csv"])
    assert ideas[0]["title"].startswith("'+Call [redacted-phone]")
    assert "[redacted-email]" in ideas[0]["title"]
    assert ideas[0]["store_name"] == "'@Formula Store"
    assert ideas[0]["ships_from"] == "DE|OTHER|PL"

    assert metadata["schema_version"] == 2
    assert metadata["export_version"] == "1.1.0"
    uuid.UUID(metadata["snapshot_id"])
    assert metadata["generated_at_utc"] == "2026-08-18T12:00:00+00:00"
    assert metadata["table_row_counts"]["sales"] == 1
    assert metadata["business_date_ranges"]["sales"]["earliest"].startswith("2026-08-17")
    assert metadata["business_date_ranges"]["sales"]["latest"].startswith("2026-08-17")
    assert metadata["privacy"]["contains_customer_pii"] is False
    assert metadata["privacy"]["contains_raw_tracking_numbers"] is False
    assert "not multiplied" in metadata["field_semantics"]["sales.csv.line_total_eur"]
    for file_name, details in metadata["files"].items():
        payload = members[f"growth_snapshot/{file_name}"]
        assert details["sha256"] == hashlib.sha256(payload).hexdigest()
        assert details["bytes"] == len(payload)


def test_multi_quantity_sale_uses_stored_line_total_once(tmp_path: Path) -> None:
    db_path = _database(tmp_path)

    payload = exporter.build_snapshot_bytes(db_path)

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        sales = _csv_rows(archive.read("growth_snapshot/sales.csv"))
        fees = _csv_rows(archive.read("growth_snapshot/fees.csv"))
        metadata = json.loads(archive.read("growth_snapshot/metadata.json"))
    assert sales[0]["quantity"] == "2"
    assert sales[0]["line_total_eur"] == "25.00"
    assert "unit_price_eur" not in sales[0]
    assert "gross_line_eur" not in sales[0]
    assert fees[0]["line_total_eur"] == "25.00"
    assert fees[0]["actual_fee_pct_of_gross"] == "30"
    assert "including allocated shipping" in metadata["field_semantics"][
        "sales.csv.line_total_eur"
    ]


def test_real_sale_and_supplier_order_statuses_are_preserved(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("UPDATE sales SET status = 'self_shipped' WHERE id = 100")
        connection.execute(
            "UPDATE orders_aliexpress SET status = 'verify_needed' WHERE id = 200"
        )
        connection.commit()
    finally:
        connection.close()

    payload = exporter.build_snapshot_bytes(db_path)

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        sales = _csv_rows(archive.read("growth_snapshot/sales.csv"))
        supplier_orders = _csv_rows(archive.read("growth_snapshot/supplier_orders.csv"))
    assert sales[0]["status"] == "self_shipped"
    assert supplier_orders[0]["status"] == "verify_needed"


def test_invalid_metadata_date_fails_closed_without_leaking_value(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    invalid_value = "2026-08-15 PRIVATE_LEGACY_VALUE"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "UPDATE price_history SET created_at = ? WHERE id = 600",
            (invalid_value,),
        )
        connection.commit()
    finally:
        connection.close()
    output = tmp_path / "must-not-exist.zip"

    with pytest.raises(exporter.SnapshotExportError) as exc_info:
        exporter.write_snapshot(db_path, output)

    assert invalid_value not in str(exc_info.value)
    assert not output.exists()


def test_unexpected_sensitive_column_fails_closed(tmp_path: Path) -> None:
    db_path = _database(tmp_path, populated=False)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("ALTER TABLE sales ADD COLUMN customer_secret TEXT")
        connection.commit()
    finally:
        connection.close()
    output = tmp_path / "must-not-exist.zip"

    with pytest.raises(exporter.UnsafeSchemaError):
        exporter.write_snapshot(db_path, output)

    assert not output.exists()


def test_unreviewed_non_sensitive_column_also_fails_closed(tmp_path: Path) -> None:
    db_path = _database(tmp_path, populated=False)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("ALTER TABLE listings ADD COLUMN future_metric INTEGER")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(exporter.SchemaValidationError):
        exporter.build_snapshot_bytes(db_path)


def test_known_legacy_image_audit_column_is_accepted_but_never_exported(
    tmp_path: Path,
) -> None:
    db_path = _database(tmp_path, populated=False)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("ALTER TABLE listings ADD COLUMN image_audit_at TEXT")
        connection.execute(
            "INSERT INTO listings (id, title_seo, image_audit_at) VALUES (?, ?, ?)",
            (1, "Safe listing", "PRIVATE_LEGACY_IMAGE_AUDIT_MARKER"),
        )
        connection.commit()
    finally:
        connection.close()

    payload = exporter.build_snapshot_bytes(db_path)

    assert b"image_audit_at" not in payload
    assert b"PRIVATE_LEGACY_IMAGE_AUDIT_MARKER" not in payload
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert tuple(archive.namelist()) == exporter.EXPECTED_ARCHIVE_FILES


def test_missing_required_table_fails_without_output(tmp_path: Path) -> None:
    db_path = _database(tmp_path, populated=False)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("DROP TABLE sales")
        connection.commit()
    finally:
        connection.close()
    output = tmp_path / "must-not-exist.zip"

    with pytest.raises(exporter.SchemaValidationError):
        exporter.write_snapshot(db_path, output)

    assert not output.exists()


def test_missing_required_column_fails_without_output(tmp_path: Path) -> None:
    db_path = _database(tmp_path, populated=False)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("ALTER TABLE sales DROP COLUMN price_eur")
        connection.commit()
    finally:
        connection.close()
    output = tmp_path / "must-not-exist.zip"

    with pytest.raises(exporter.SchemaValidationError):
        exporter.write_snapshot(db_path, output)

    assert not output.exists()


def test_export_contract_matches_real_application_orm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Import application models only after moving away from the repository root,
    # so application settings cannot discover the repository's .env file.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    from sqlalchemy import create_engine

    from app.models import Base

    db_path = tmp_path / "orm-schema.db"
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(db_path))
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    output = tmp_path / "orm-snapshot.zip"
    exporter.write_snapshot(db_path, output, temp_parent=tmp_path)

    assert output.exists()
    with zipfile.ZipFile(output) as archive:
        assert tuple(archive.namelist()) == exporter.EXPECTED_ARCHIVE_FILES


def test_output_can_never_replace_source_database(tmp_path: Path) -> None:
    db_path = _database(tmp_path, populated=False)
    before = db_path.read_bytes()

    with pytest.raises(exporter.SnapshotExportError):
        exporter.write_snapshot(db_path, db_path)

    assert db_path.read_bytes() == before


def test_empty_tables_degrade_to_headers_and_metadata_warnings(tmp_path: Path) -> None:
    db_path = _database(tmp_path, populated=False)
    payload = exporter.build_snapshot_bytes(
        db_path,
        generated_at=datetime(2026, 8, 18, tzinfo=timezone.utc),
    )
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert tuple(archive.namelist()) == exporter.EXPECTED_ARCHIVE_FILES
        metadata = json.loads(archive.read("growth_snapshot/metadata.json"))
        for file_name, expected_headers in exporter.OUTPUT_HEADERS.items():
            content = archive.read(f"growth_snapshot/{file_name}").decode("utf-8")
            assert tuple(next(csv.reader(io.StringIO(content)))) == expected_headers
            assert len(content.splitlines()) == 1
        assert all(count == 0 for count in metadata["table_row_counts"].values())
        assert "empty_table:sales" in metadata["warnings"]
        assert metadata["business_date_ranges"]["sales"] == {
            "total": 0, "populated": 0, "earliest": None, "latest": None,
        }


def test_workflow_is_manual_fixed_and_short_lived() -> None:
    """Sicherheitsregeln fuer den Snapshot-Workflow.

    Die Datei aus dem Ursprungsprojekt wurde entfernt: sie zog Daten vom
    Produktions-Server der GbR (eigener SSH-Schluessel, fremder Host). Fuer
    Druckhelden muss ein eigener Workflow her, wenn er gebraucht wird.

    Der Test bleibt als Waechter stehen und greift automatisch wieder, sobald
    eine eigene Datei angelegt wird - dann muessen diese Regeln erfuellt sein.
    """
    if not WORKFLOW.exists():
        pytest.skip("Kein eigener Snapshot-Workflow angelegt (GbR-Datei entfernt).")
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"(?m)^\s{2}workflow_dispatch:\s*$", workflow)
    assert not re.search(r"(?m)^\s{2}(push|pull_request|schedule):", workflow)
    assert "contents: read" in workflow
    assert "scripts/export_growth_snapshot.py" in workflow
    assert "scripts/dump_order_errors.py" not in workflow
    assert "--stdout" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "retention-days: 7" in workflow
    assert "cat " not in workflow
    stream_index = workflow.index("- name: Stream sanitized snapshot from production")
    cleanup_index = workflow.index("- name: Remove VPS transport credentials")
    validation_index = workflow.index("- name: Validate sanitized artifact structure")
    upload_index = workflow.index("- name: Upload private short-lived artifact")
    assert stream_index < cleanup_index < validation_index < upload_index
    pre_upload_cleanup = workflow[cleanup_index:upload_index]
    assert 'rm -f "$HOME/.ssh/id_deploy"' in pre_upload_cleanup
    assert 'rm -f "$HOME/.ssh/config"' in pre_upload_cleanup
