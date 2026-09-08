"""Blocklist-basierter IP-/Content-Filter.

Prueft Text (Motiv-Beschreibung + Overlay) VOR der Generierung gegen eine Sperrliste.
Normalisiert den Text und erkennt einfache Umgehungen (Leerzeichen/Sonderzeichen
zwischen Buchstaben).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

from app.studio.safety.errors import FilterFehler as PodError

_ALLOWED = re.compile(r"[^0-9a-zäöüß ]+")
_WS = re.compile(r"\s+")
_MIN_COLLAPSED = 4


@dataclass(frozen=True)
class FilterResult:
    """Ergebnis einer Pruefung."""

    allowed: bool
    reason: str | None = None
    matches: tuple[str, ...] = ()


class IPFilter:
    """Prueft Texte gegen eine kategorisierte Sperrliste."""

    def __init__(self, terms: dict[str, list[str]]) -> None:
        self._by_term: dict[str, str] = {}
        for category, items in (terms or {}).items():
            for item in items or []:
                norm = _normalize(str(item))
                if norm:
                    self._by_term[norm] = category

    @classmethod
    def from_file(cls, path: str | Path) -> IPFilter:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise PodError("Blocklist muss ein Mapping Kategorie -> Liste sein.")
        return cls(data)

    @classmethod
    def default(cls) -> IPFilter:
        with resources.files("pod.safety").joinpath("blocklist.yaml").open(
            "r", encoding="utf-8"
        ) as fh:
            data = yaml.safe_load(fh) or {}
        return cls(data)

    def check(self, *texts: str | None) -> FilterResult:
        """Blockiert, sobald ein Sperrbegriff im kombinierten Text auftaucht."""
        combined = " ".join(t for t in texts if t)
        spaced = _normalize(combined)
        collapsed = spaced.replace(" ", "")

        matched: list[str] = []
        for term, category in self._by_term.items():
            if _matches(term, spaced, collapsed):
                matched.append(f"{category}:{term}")

        if matched:
            return FilterResult(
                allowed=False,
                reason="Gesperrter Inhalt: " + ", ".join(sorted(matched)),
                matches=tuple(sorted(matched)),
            )
        return FilterResult(allowed=True)


def _normalize(text: str) -> str:
    return _WS.sub(" ", _ALLOWED.sub(" ", text.lower())).strip()


def _matches(term: str, spaced: str, collapsed: str) -> bool:
    # Ganzwort-/Phrasentreffer im normalisierten Text.
    if f" {term} " in f" {spaced} ":
        return True
    # Umgehung via eingeschobener Zeichen ("d i s n e y") -> zusammengezogen pruefen.
    term_collapsed = term.replace(" ", "")
    return len(term_collapsed) >= _MIN_COLLAPSED and term_collapsed in collapsed
