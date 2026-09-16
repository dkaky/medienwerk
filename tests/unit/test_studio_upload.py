"""Studio-Uploads: eigene Bilder und PDFs werden zu Motiven."""
from __future__ import annotations

import json
from io import BytesIO

import pytest
from PIL import Image

from app.studio import router, service


def _png_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGBA", (24, 24), (20, 40, 60, 255)).save(buf, "PNG")
    return buf.getvalue()


class _Upload:
    def __init__(self, filename: str, data: bytes, content_type: str):
        self.filename = filename
        self._data = data
        self.content_type = content_type

    async def read(self) -> bytes:
        return self._data


@pytest.mark.asyncio
async def test_bild_upload_wird_png_motiv(db, tmp_path, monkeypatch):
    monkeypatch.setattr(router.get_settings(), "studio_image_dir", str(tmp_path))
    upload = _Upload("mein-bild.png", _png_bytes(), "image/png")

    design = await router._upload_ablegen(db, datei=upload, titel="Eigenes Bild")

    assert design.title == "Eigenes Bild"
    assert design.source == "upload"
    assert design.image_url.startswith("/studio/bilder/uploads/")
    assert (tmp_path / design.image_url.removeprefix("/studio/bilder/")).is_file()
    meta = json.loads(design.meta_json)
    assert meta["upload"]["typ"] == "bild"


@pytest.mark.asyncio
async def test_pdf_upload_erzeugt_vorschau_und_loescht_original_mit(db, tmp_path, monkeypatch):
    monkeypatch.setattr(router.get_settings(), "studio_image_dir", str(tmp_path))
    upload = _Upload("druckvorlage.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")

    design = await router._upload_ablegen(db, datei=upload, titel=None)
    meta = json.loads(design.meta_json)
    original = tmp_path / meta["upload"]["original_url"].removeprefix("/studio/dateien/")
    vorschau = tmp_path / design.image_url.removeprefix("/studio/bilder/")

    assert design.title == "druckvorlage"
    assert meta["upload"]["typ"] == "pdf"
    assert original.is_file()
    assert vorschau.is_file()

    bericht = service.delete_design(db, design_id=design.id, bildordner=tmp_path)

    assert bericht["bild_geloescht"] is True
    assert bericht["original_geloescht"] is True
    assert not original.exists()
    assert not vorschau.exists()
