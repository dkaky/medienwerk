"""Unit-Tests: Modelle/Constraints und LLM-Beschreibungsbereinigung."""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.integrations.llm import MockLLMClient, _clean_description
from app.models import Product


def test_product_unique_url(db):
    db.add(Product(aliexpress_url="https://de.aliexpress.com/item/1.html"))
    db.commit()
    db.add(Product(aliexpress_url="https://de.aliexpress.com/item/1.html"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_clean_description_removes_china_hints():
    raw = "Great product 中国发货 free shipping 15-30 days cheap from China factory"
    cleaned, warnings = _clean_description(raw)
    assert "中国" not in cleaned
    assert "china" not in cleaned.lower()
    assert "15-30" not in cleaned
    assert warnings  # es wurde mindestens ein Muster entfernt


@pytest.mark.asyncio
async def test_llm_title_max_80_chars():
    llm = MockLLMClient()
    gen = await llm.generate_listing(
        title_raw="X" * 200, description_raw="some clean text", category_guess="123"
    )
    assert len(gen.title_seo) <= 80


@pytest.mark.asyncio
async def test_mock_listing_format_has_blocks_and_footer():
    gen = await MockLLMClient().generate_listing(
        title_raw="Wireless Headphones", description_raw="Great sound", category_guess="123"
    )
    # Pflicht-Footer + Emoji-Blockstruktur + strategischer Hinweis
    assert "§ 19 Abs. 1 UStG" in gen.description_clean
    assert "✨ Auf einen Blick" in gen.description_clean
    assert "**" not in gen.description_clean  # keine Markdown-Sternchen (eBay = HTML)
    assert gen.strategic_note.startswith("[MOCK")


@pytest.mark.asyncio
async def test_mock_jewelry_disclaimer():
    gen = await MockLLMClient().generate_listing(
        title_raw="Damen Armband Gold Edelstahl", description_raw="schmuck kette",
        category_guess="123",
    )
    assert "Modeschmuck, kein Echtgold oder Echtsilber" in gen.description_clean


@pytest.mark.asyncio
async def test_mock_flags_vero_brand():
    gen = await MockLLMClient().generate_listing(
        title_raw="Charm passend für Pandora Armband", description_raw="silber",
        category_guess="123",
    )
    assert "Markenrisiko" in gen.strategic_note
    assert "pandora" in gen.strategic_note.lower()


def test_error_text_never_empty():
    """Regression: httpx-Timeouts haben str(exc)=='' -> repr als Fallback."""
    from app.services.common import error_text

    assert error_text(ValueError("kaputt")) == "kaputt"
    assert error_text(TimeoutError()) == "TimeoutError()"
    assert error_text(ValueError("  ")) == "ValueError('  ')"


def test_cleanup_stale_tasks(db):
    """Regression: haengende in_progress-Tasks werden beim Start als failed markiert."""
    from datetime import datetime, timedelta, timezone

    from app.models import TaskLog
    from app.services.common import cleanup_stale_tasks

    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)
    zombie = TaskLog(task_type="upload", reference_id="zombie", status="in_progress")
    fresh = TaskLog(task_type="upload", reference_id="frisch", status="in_progress")
    done = TaskLog(task_type="upload", reference_id="fertig", status="success")
    db.add_all([zombie, fresh, done])
    db.commit()
    zombie.created_at = old
    db.commit()

    assert cleanup_stale_tasks(db) == 1
    db.refresh(zombie); db.refresh(fresh); db.refresh(done)
    assert zombie.status == "failed" and "Auto-Cleanup" in zombie.error_message
    assert fresh.status == "in_progress"
    assert done.status == "success"
