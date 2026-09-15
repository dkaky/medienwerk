"""Verstaendliche Meldungen, wenn OpenAI eine Bilderzeugung ablehnt - ohne Netz."""
from __future__ import annotations

import sys
import types

import pytest

from app.studio.generation.base import ImageRequest
from app.studio.generation.openai_provider import OpenAIProvider


class _Abgelehnt(Exception):
    def __init__(self, status_code, code):
        super().__init__(f"Error code: {status_code} - {code}")
        self.status_code, self.code = status_code, code


def _mit_fehler(monkeypatch, status, code):
    class _Bilder:
        def generate(self, **kw):
            raise _Abgelehnt(status, code)

    class _Client:
        def __init__(self, **kw):
            self.images = _Bilder()

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=_Client))


@pytest.mark.parametrize("status, code, erwartet", [
    (429, "rate_limit_exceeded", "zu viele Anfragen"),
    (429, "insufficient_quota", "Guthaben"),
    (401, None, "API-Schluessel abgelehnt"),
])
def test_ablehnung_wird_verstaendlich(monkeypatch, status, code, erwartet):
    _mit_fehler(monkeypatch, status, code)
    with pytest.raises(RuntimeError, match=erwartet):
        OpenAIProvider("sk-test").generate(ImageRequest(prompt="Berg", width=1024, height=1024))
