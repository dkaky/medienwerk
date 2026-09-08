"""Bild-Anbieter GPT Image 2.

Starke Textwiedergabe und echte Transparenz - deshalb im Vorprojekt die erste
Wahl fuer Spruch- und Textmotive.

Uebernommen aus POD-Shop (src/pod/generation/openai_provider.py), mit zwei
Aenderungen:

* Kein Konfigurations-Objekt mehr im Konstruktor, sondern die zwei Werte, die
  wirklich gebraucht werden. So laesst sich der Anbieter testen, ohne die halbe
  Anwendung hochzufahren.
* Die Kosten werden gemeldet. Im Vorprojekt blieben sie leer, damit war die
  Kostenbremse auf diesen Anbieter blind.
"""

from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image

from app.studio.generation.base import SUPPORTED_SIZES, GeneratedImage, ImageRequest

_SIZE_ARG = {
    "square": "1024x1024",
    "portrait": "1024x1536",
    "landscape": "1536x1024",
}

# Listenpreise je Bild (Stand 08/2026, grosszuegig aufgerundet). Lieber etwas zu
# hoch ansetzen: die Kostenbremse soll frueher greifen, nicht spaeter.
_PREIS_USD = {"low": 0.02, "medium": 0.04, "high": 0.08, "auto": 0.08}


class OpenAIProvider:
    """Bindet ``images.generate`` von GPT Image 2 an."""

    name = "openai"
    model = "gpt-image-2"

    def __init__(self, api_key: str, quality: str = "medium") -> None:
        if not api_key:
            raise ValueError("Kein OpenAI-Schluessel gesetzt (OPENAI_API_KEY in der .env).")
        self._api_key = api_key
        self._quality = quality or "medium"

    def geschaetzte_kosten(self) -> float:
        """Was ein Bild voraussichtlich kostet - fuer die Vorabpruefung."""
        return _PREIS_USD.get(self._quality, 0.08)

    def generate(self, request: ImageRequest) -> GeneratedImage:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - optionale Abhaengigkeit
            raise RuntimeError("openai-SDK nicht installiert (pip install openai).") from exc

        orientation = request.orientation()
        # Keine automatischen Wiederholungen einer eventuell schon bezahlten Erzeugung.
        client = OpenAI(api_key=self._api_key, timeout=180.0, max_retries=0)
        try:
            result = client.images.generate(
                model=self.model,
                prompt=request.prompt,
                size=_SIZE_ARG[orientation],
                quality=self._quality,
                background="transparent" if request.transparent else "opaque",
                output_format="png",
                n=1,
            )
        except Exception as exc:  # SDK- und HTTP-Fehler einheitlich kapseln
            # Manche Auth-Fehler enthalten Teile des Schluessels. Nicht an Browser/Log geben.
            status = getattr(exc, "status_code", None)
            code = getattr(exc, "code", None)
            if status == 401:
                detail = "OpenAI hat den API-Schluessel abgelehnt. OPENAI_API_KEY pruefen."
            elif code in {"insufficient_quota", "billing_hard_limit_reached", "billing_limit_user_error"}:
                detail = "OpenAI-API-Guthaben oder Kontolimit erschoepft. Das lokale Tagesbudget ist davon unabhaengig."
            elif status == 403:
                detail = "OpenAI verweigert den Modellzugriff. Modellfreigabe und Organisationsverifizierung pruefen."
            else:
                detail = str(exc).replace(self._api_key, "[API-Schluessel entfernt]")
            raise RuntimeError(f"GPT Image 2 Aufruf fehlgeschlagen: {detail}") from exc
        finally:
            client.close()

        payload = result.data[0].b64_json if result.data else None
        if not payload:
            raise RuntimeError("GPT Image 2 lieferte kein Bild zurueck.")
        img = Image.open(BytesIO(base64.b64decode(payload))).convert("RGBA")
        if img.size != SUPPORTED_SIZES[orientation]:
            img = img.resize(SUPPORTED_SIZES[orientation])

        return GeneratedImage(
            image=img,
            provider=self.name,
            model=self.model,
            cost_usd=self.geschaetzte_kosten(),
        )
