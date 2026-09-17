import json

from app.studio import zielgruppe


class _Design:
    def __init__(self, title, beschreibung):
        self.title = title
        self.meta_json = json.dumps({
            "radar": {
                "thema": title,
                "beschreibung": beschreibung,
                "stichworte": [],
            }
        }, ensure_ascii=False)


def test_mama_papa_spruch_wird_als_kind_auf_kinder_tshirt_geplant():
    plan = zielgruppe.plan(_Design(
        "Mama ist besser als Papa",
        {"spruch": "Mama ist besser als Papa", "zielgruppe": "Kinder"},
    ))

    assert plan.zielgruppe == "kind"
    assert plan.blockiert is False
    assert plan.produkte == ["kids_tshirt"]
    assert plan.abteilung == "Unisex Kinder"


def test_ehemann_spruch_geht_als_frauen_signal_auf_tshirt():
    plan = zielgruppe.plan(_Design(
        "Mein Ehemann ist toll",
        {"spruch": "Mein Ehemann ist toll", "zielgruppe": "Ehefrauen"},
    ))

    assert plan.zielgruppe == "frau"
    assert plan.blockiert is False
    assert plan.produkte == ["tshirt"]
    assert plan.abteilung == "Damen"


def test_kaffee_spruch_bleibt_im_auto_modus_tshirt():
    plan = zielgruppe.plan(_Design(
        "Ohne Kaffee ohne mich",
        {"spruch": "Ohne Kaffee ohne mich"},
    ))

    assert plan.produkte == ["tshirt"]
    assert plan.abteilung == "Unisex Erwachsene"


def test_auto_modus_nimmt_keine_sonderprodukte():
    faelle = [
        ("Kalter Winter Hoodie Spruch", {"spruch": "Kalter Winter Hoodie Spruch"}),
        ("Streetwear Oversize locker", {"spruch": "Streetwear Oversize locker"}),
        ("Büro Polo Teamleiter", {"spruch": "Büro Polo Teamleiter"}),
        ("Kaffee Tasse Spruch", {"spruch": "Kaffee Tasse Spruch"}),
    ]

    for titel, beschreibung in faelle:
        plan = zielgruppe.plan(_Design(titel, beschreibung))
        assert plan.produkte == ["tshirt"]
        assert plan.abteilung == "Unisex Erwachsene"
