"""prune_old_ideas: alte UNBEARBEITETE Ideen loeschen, gemerkte/importierte behalten."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, func

from app.models import ProductIdea
from app.services import product_research_service as r


def _mk(db, *, status, age_days, aid):
    idea = ProductIdea(aliexpress_id=aid, title=f"x{aid}", status=status)
    db.add(idea)
    db.flush()
    idea.created_at = datetime.utcnow() - timedelta(days=age_days)
    db.commit()
    return idea


def test_prune_deletes_only_old_unhandled(db):
    _mk(db, status="new", age_days=10, aid="a1")        # alt + offen -> weg
    _mk(db, status="rejected", age_days=8, aid="a2")    # alt + verworfen -> weg
    _mk(db, status="new", age_days=2, aid="a3")         # jung -> bleibt
    _mk(db, status="kept", age_days=30, aid="a4")       # gemerkt -> bleibt IMMER
    _mk(db, status="imported", age_days=30, aid="a5")   # importiert -> bleibt

    res = r.prune_old_ideas(db, days=5)
    assert res["deleted"] == 2
    remaining = {x.aliexpress_id for x in db.scalars(select(ProductIdea)).all()}
    assert remaining == {"a3", "a4", "a5"}


def test_prune_respects_days(db):
    _mk(db, status="new", age_days=4, aid="b1")
    _mk(db, status="new", age_days=6, aid="b2")
    res = r.prune_old_ideas(db, days=5)
    assert res["deleted"] == 1
    assert db.scalar(select(func.count()).select_from(ProductIdea)) == 1


def test_prune_negative_or_zero_days_never_nukes_recent(db):
    """Review-Fund: negatives/0-days darf NIE frische Ideen loeschen (Floor auf 1 Tag)."""
    _mk(db, status="new", age_days=0, aid="c1")     # gerade erst erstellt
    for bad in (-1, 0, -99):
        assert r.prune_old_ideas(db, days=bad)["deleted"] == 0
    assert db.scalar(select(func.count()).select_from(ProductIdea)) == 1
