"""Regression checks for the complete dashboard and explicitly unlimited images."""
from types import SimpleNamespace

import pytest
from PIL import Image

from app.config import get_settings
from app.studio import kosten
from app.studio.generation import service as generation
from app.studio.generation.base import GeneratedImage


@pytest.fixture
def unlimited_studio(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "studio_enabled", True)
    monkeypatch.setattr(settings, "studio_daily_budget_usd", -1.0)
    monkeypatch.setattr(settings, "studio_image_dir", str(tmp_path / "images"))
    return settings


def test_unlimited_keeps_accounting_without_blocking(db, unlimited_studio):
    kosten.verbuche(db, provider="openai", kosten_usd=200.0)
    kosten.pruefe(db, 1000.0)
    assert kosten.rest_heute(db) is None
    assert kosten.verbraucht_heute(db) == pytest.approx(200.0)


def test_unlimited_status_serializes(client, unlimited_studio):
    response = client.get("/api/v1/studio/status")
    assert response.status_code == 200
    assert response.json()["tagesbudget_usd"] == -1
    assert response.json()["rest_heute_usd"] is None


def test_unlimited_paid_generation_saves_and_returns_image(client, db, unlimited_studio, monkeypatch):
    # Simulates a paid provider without making an external or billable request.
    provider = SimpleNamespace(
        geschaetzte_kosten=lambda: 0.08,
        generate=lambda request: GeneratedImage(
            image=Image.new("RGBA", (1024, 1024), (20, 80, 150, 200)),
            provider="openai", model="test-only", cost_usd=0.08,
        ),
    )
    monkeypatch.setattr(generation, "waehle_anbieter", lambda name: provider)
    response = client.post("/api/v1/studio/generate", json={
        "prompt": "Eine eigenstaendige Bergillustration", "anbieter": "openai",
    })
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["rest_budget_usd"] is None
    assert result["anbieter"] == "openai"
    assert kosten.verbraucht_heute(db) == pytest.approx(0.08)
    assert client.get(result["design"]["image_url"]).status_code == 200
    assert len(client.get("/api/v1/studio/designs").json()) == 1


@pytest.mark.parametrize("url", ["/", "/dashboard"])
def test_full_dashboard_is_restored(client, url):
    response = client.get(url)
    assert response.status_code == 200
    for section in ("overview", "products", "pricecheck", "orders", "optimization", "belege"):
        assert f'data-view="{section}"' in response.text
    assert 'href="/studio"' in response.text
    assert "Medienwerk" in response.text


def test_studio_links_back_to_original_business_sections(client):
    html = client.get("/studio").text
    for section in ("products", "pricecheck", "orders", "optimization", "belege"):
        assert f'href="/#{section}"' in html


def test_radar_result_allows_unlimited_budget():
    from app.studio.schemas import NutzenOut

    result = NutzenOut.model_validate({
        "idee": {"id": 1, "quelle_plattform": "ebay", "fremdtitel": "Berge",
                 "status": "uebernommen"},
        "design_id": 1, "bild_url": "/studio/bilder/test.png", "prompt": "Berge",
        "kosten_usd": 0.08, "rest_budget_usd": None,
    })
    assert result.model_dump(mode="json")["rest_budget_usd"] is None
