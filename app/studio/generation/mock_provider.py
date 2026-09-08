"""Deterministischer Offline-Provider.

Erzeugt ohne Netz/API ein reproduzierbares RGBA-Platzhalterbild aus dem Prompt.
Gleiche Anfrage -> exakt gleiches Bild. Dient Entwicklung und Tests der gesamten
Pipeline, bevor echte API-Zugaenge existieren.
"""

from __future__ import annotations

import hashlib
import textwrap

from PIL import Image, ImageDraw

from app.studio.generation.base import GeneratedImage, ImageRequest


class MockProvider:
    """Malt einen farbigen Block plus (gekuerzten) Prompt-Text."""

    name = "mock"
    model = "mock-v1"

    def generate(self, request: ImageRequest) -> GeneratedImage:
        digest = hashlib.sha256(
            f"{request.prompt}|{request.seed}|{request.width}x{request.height}".encode()
        ).digest()
        color = (digest[0], digest[1], digest[2], 255)

        bg = (0, 0, 0, 0) if request.transparent else (255, 255, 255, 255)
        img = Image.new("RGBA", (request.width, request.height), bg)
        draw = ImageDraw.Draw(img)

        # Zentraler Block als Platzhalter-Motiv (deterministische Groesse/Position).
        margin_x = request.width // 8
        margin_y = request.height // 8
        draw.rectangle(
            [margin_x, margin_y, request.width - margin_x, request.height - margin_y],
            fill=color,
        )

        # Prompt-Text zur Sichtkontrolle (Default-Font, kein externer Asset noetig).
        wrapped = textwrap.fill(request.prompt[:120], width=24)
        draw.multiline_text(
            (margin_x + 20, margin_y + 20),
            wrapped,
            fill=(255, 255, 255, 255),
        )

        return GeneratedImage(image=img, provider=self.name, model=self.model, cost_usd=0.0)
