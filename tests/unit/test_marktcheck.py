import json

import pytest

from app.studio.models import MotivIdee
from app.studio.radar import marktcheck


@pytest.mark.asyncio
async def test_marktcheck_speichert_verkaufssignal(db):
    idee = MotivIdee(
        quelle_plattform="trend",
        quelle_shop="web",
        fremd_id="spruch-1",
        fremdtitel="",
        thema="Mama Spruch",
        status="neu",
        beschreibung=json.dumps({"spruch": "Mama du bist mein Lieblingsmensch"}),
    )
    db.add(idee)
    db.commit()

    async def aktive(_suchbegriff):
        return 42

    async def verkauft(_suchbegriff):
        return 8, 23

    bericht = await marktcheck.pruefe(
        db, aktive_angebote=aktive, verkaufte_suche=verkauft
    )

    db.refresh(idee)
    daten = json.loads(idee.beschreibung)
    assert bericht["geprueft"] == 1
    assert daten["marktcheck"]["bewertung"] in {"mittel", "stark"}
    assert daten["marktcheck"]["aktive_angebote"] == 42
    assert daten["marktcheck"]["verkaufte_treffer"] == 8
    assert idee.signal is not None
    assert "Marktcheck" in idee.signal_grund


def test_bewertung_unterscheidet_nachfrage_von_unbekannt():
    stark, label = marktcheck.bewertung(
        marktcheck.MarktSignal("Papa Spruch", aktive_angebote=20,
                               verkaufte_treffer=10, verkauft_summe=80)
    )
    unbekannt, unbekannt_label = marktcheck.bewertung(
        marktcheck.MarktSignal("Nische", aktive_angebote=None,
                               verkaufte_treffer=None, verkauft_summe=None)
    )

    assert stark > 70
    assert label == "stark"
    assert unbekannt is None
    assert unbekannt_label == "unbekannt"
