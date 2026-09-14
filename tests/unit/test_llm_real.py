"""Unit-Tests fuer RealLLMClient: Fehler-Mapping + Provider-Auswahl (ohne Netzwerk).

Echte API-Aufrufe brauchen Keys und werden nicht getestet; hier verifizieren wir
die deterministische Logik (Exception-Uebersetzung, Titel-Grenze, Lazy-Import).
"""
from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.integrations.llm import RealLLMClient
from app.retry import PersistentError, RateLimitError, TransientError


def _client(provider: str = "claude") -> RealLLMClient:
    return RealLLMClient(Settings(llm_provider=provider, llm_api_key="x", use_mocks=False))


def test_enforce_title_truncates_to_80():
    warnings: list[str] = []
    out = RealLLMClient._enforce_title("X" * 200, warnings)
    assert len(out) == 80
    assert warnings  # Kuerzungs-Hinweis gesetzt


def test_enforce_title_warns_when_too_short():
    warnings: list[str] = []
    RealLLMClient._enforce_title("Kurzer Titel", warnings, warn_below=60)
    assert any("Keyword-Potenzial" in w for w in warnings)
    # Langer Titel (>= Schwelle) -> keine Warnung
    warnings2: list[str] = []
    RealLLMClient._enforce_title("X" * 78, warnings2, warn_below=60)
    assert warnings2 == []


def test_mine_keywords_extracts_frequent_and_drops_floskeln():
    titles = [
        "Edelstahl Halskette Herren Silber Premium",
        "Halskette Edelstahl Damen Silber Top Qualität",
        "Armband Leder Herren",
    ]
    kw = RealLLMClient._mine_keywords(titles, top=6)
    assert "halskette" in kw and "edelstahl" in kw and "silber" in kw  # >=2x
    assert "premium" not in kw and "top" not in kw and "qualität" not in kw  # Floskeln raus


def test_translate_rate_limit():
    import anthropic

    req = httpx.Request("POST", "https://api.anthropic.com")
    resp = httpx.Response(429, headers={"retry-after": "7"}, request=req)
    exc = anthropic.RateLimitError("slow down", response=resp, body=None)
    mapped = _client()._translate(exc)
    assert isinstance(mapped, RateLimitError)
    assert mapped.retry_after == 7.0


def test_translate_bad_request_is_persistent():
    import anthropic

    req = httpx.Request("POST", "https://api.anthropic.com")
    resp = httpx.Response(400, request=req)
    exc = anthropic.BadRequestError("bad", response=resp, body=None)
    assert isinstance(_client()._translate(exc), PersistentError)


def test_translate_unknown_is_transient():
    assert isinstance(_client()._translate(ValueError("boom")), TransientError)


def test_lazy_clients_constructed_on_demand():
    c = _client("claude")
    assert c._anthropic is None  # nichts beim Init
    assert c._anthropic_client() is not None
    assert c._anthropic is not None  # gecacht


def test_enforce_title_trims_at_word_boundary():
    warnings = []
    # 85 Zeichen, Cut bei 80 läge mitten in "Kette" -> soll aufs letzte ganze Wort zurück
    long = "Deutscher Adler Halskette Edelstahl Anhänger Herren quadratisch Silber Gold Kette xx"
    out = RealLLMClient._enforce_title(long, warnings)
    assert len(out) <= 80 and not out.endswith("Kett") and " " in out
    assert out == out.rstrip() and warnings
    # Erstes Wort länger als 80 (keine Grenze) -> harter Cut bleibt
    assert len(RealLLMClient._enforce_title("X"*200, [])) == 80


# ------------------------- KI-Bildbewertung -------------------------
async def test_real_assess_images_empty_without_network():
    """Ohne Bilder kein API-Call: leeres, wohlgeformtes Ergebnis."""
    r = await _client().assess_images(product_title="Tasche", image_urls=[])
    assert r == {"assessments": [], "best_index": None, "note": ""}


async def test_mock_assess_images_scores_and_best():
    from app.integrations.llm import MockLLMClient
    r = await MockLLMClient().assess_images(product_title="Reisetasche",
                                            image_urls=["a", "b", "c"])
    assert [a["index"] for a in r["assessments"]] == [0, 1, 2]
    assert r["best_index"] == 0                                  # Index 0 = aktuelles Titelbild
    assert r["assessments"][0]["score"] >= r["assessments"][1]["score"]
    assert all(0.0 <= a["score"] <= 10.0 for a in r["assessments"])


async def test_base_assess_images_default_empty():
    """Basis-Client (kein Vision) liefert leeres Default-Ergebnis."""
    from app.integrations.llm import LLMClient

    class _Bare(LLMClient):
        async def generate_listing(self, **kw): ...
        async def suggest_title(self, **kw): ...

    r = await _Bare().assess_images(product_title="X", image_urls=["a"])
    assert r == {"assessments": [], "best_index": None, "note": ""}


async def test_real_assess_images_empty_assessments_yields_no_best(monkeypatch):
    """Wohlgeformtes Schema mit leerer Bewertungsliste + Default best_index=0 darf KEIN
    'bestes' Bild markieren (sonst 🏆 auf einem unbewerteten Bild)."""
    from app.integrations.llm import _ImgAssessmentList
    c = _client()

    class _Resp:
        parsed_output = _ImgAssessmentList(assessments=[], best_index=0)

    class _Msgs:
        async def parse(self, **kw):
            return _Resp()

    class _Client:
        messages = _Msgs()

    monkeypatch.setattr(c, "_anthropic_client", lambda: _Client())
    r = await c.assess_images(product_title="X", image_urls=["u1", "u2"])
    assert r["assessments"] == [] and r["best_index"] is None
