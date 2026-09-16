"""Studio-Motive loeschen: nur unverknuepfte Entwuerfe verschwinden hart."""
from __future__ import annotations

import pytest

from app.studio import service
from app.studio.models import MotivIdee, StudioDesign


def test_unverknuepftes_motiv_loescht_datenbankzeile_und_bild(db, tmp_path):
    bild = tmp_path / "motiv.png"
    bild.write_bytes(b"png")
    design = service.create_design(
        db, title="Fehlversuch", source="mock", image_url="/studio/bilder/motiv.png"
    )

    bericht = service.delete_design(db, design_id=design.id, bildordner=tmp_path)

    assert bericht == {
        "design_id": design.id,
        "geloescht": True,
        "bild_geloescht": True,
        "original_geloescht": False,
    }
    assert db.get(StudioDesign, design.id) is None
    assert not bild.exists()


def test_verknuepftes_radar_motiv_wird_nicht_hart_geloescht(db, tmp_path):
    bild = tmp_path / "motiv.png"
    bild.write_bytes(b"png")
    design = service.create_design(
        db, title="Aus Radar", source="mock", image_url="/studio/bilder/motiv.png"
    )
    db.add(MotivIdee(
        quelle_plattform="trend",
        quelle_shop="websuche",
        fremdtitel="Beleg",
        status="uebernommen",
        design_id=design.id,
    ))
    db.commit()

    with pytest.raises(service.StudioFehler, match="Radar-Ideen"):
        service.delete_design(db, design_id=design.id, bildordner=tmp_path)

    assert db.get(StudioDesign, design.id) is not None
    assert bild.exists()
