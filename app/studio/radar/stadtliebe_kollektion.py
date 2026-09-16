"""Stadtliebe-Kollektion: minimalistische Motive zu den Staedten grosser Fussballvereine.

Vorgabe des Betreibers (16.09.2026): Motive fuer die Fans von Vereinen wie Bayern,
BVB, Galatasaray oder Napoli - "so weit inspirieren, wie man es darf", unbedingt
minimalistisch.

**Die Grenze.** Vereinsnamen, Wappen und Wappenteile (auch ein einzelnes "N"),
Spitznamen, Gruendungsjahre, Stadion- und Vereinssprueche sowie typische
Vereinsfarben-Paare sind markenrechtlich geschuetzt oder wirken wie Vereinsware.
Erlaubt und hier genutzt ist das, was der STADT gehoert: Wahrzeichen, Mundart,
Essen, Geschichte, Landschaft. Die Farben sind bewusst so gewaehlt, dass sie kein
Vereinsfarben-Paar der jeweiligen Stadt bilden. Stadtteile, die zugleich
Vereinsnamen sind (etwa in Istanbul), kommen nicht vor.

Jedes Motiv: ein Hauptelement, eine Linie oder wenige Flaechen, hoechstens zwei
Druckfarben, viel Freiraum. Laden kostet nichts; Geld kostet erst "Motiv erzeugen".
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from app.studio.radar import trends

KATEGORIE = "stadtliebe"

_STIL = ("Minimalistische realistische Siebdruck-Linienillustration, eine durchgehende "
         "Linie oder wenige klare Flaechen, viel Freiraum, hoechstens zwei Druckfarben")
_DRUCK = ("Eine geschlossene freigestellte Form mit gleichmaessiger Linienstaerke, ohne feine "
          "Verlaeufe, auf hellen und dunklen Textilien gut lesbar")
_RISIKO = ("Vor dem Einstellen pruefen, dass weder Vereinsfarben-Paar, Wappenform noch "
           "Vereinsschrift anklingt")
_ZEITRAUM = "Immergruen, stark vor Reisezeit, Umzug in die Stadt und zu Weihnachten"
_WARUM = ("Stadtliebe-Motive verbinden Heimatstolz mit dem Lebensgefuehl der Fans vor Ort "
          "und bleiben unabhaengig von jedem Verein verkaufbar.")
_PRODUKT = "T-Shirt, alternativ Hoodie oder Tasse"

# (stadt, thema, motiv, spruch, farben, zielgruppe, kaufmoment, verkaufswinkel, suchbegriffe)
_IDEEN: list[tuple[str, ...]] = [
    # ---------------------------------------------------------------- Muenchen
    ("München", "Zwei Türme",
     "Die beiden Kuppeltuerme der Frauenkirche als eine einzige feine durchgehende Linie, darunter "
     "eine schmale waagerechte Linie wie der Horizont, sonst nichts.",
     "Minga", ["Tannengruen", "Creme"],
     "Muenchner und Zugezogene, die ihre Stadt lieben",
     "Wird als Geschenk zum Umzug in eine Wohnung in Muenchen gekauft",
     "Das bekannteste Stadtprofil in einer Linie statt Oktoberfest-Klischee",
     ["Muenchen Shirt minimalistisch", "Minga Shirt", "Muenchen Geschenk"]),
    ("München", "Brezn",
     "Eine einzelne Brezn aus einer durchgehenden Linie gezeichnet, mit drei angedeuteten "
     "Salzkoernern, mittig freigestellt und sehr ruhig.",
     "Servus", ["Laugenbraun", "Creme"],
     "Muenchen-Liebhaber, die zum Fruehstueck eine Brezn brauchen",
     "Wird als Tasse fuer den Kollegen mit Muenchner Wurzeln verschenkt",
     "Bayerisches Alltagsritual als ruhige Grafik statt lauter Tracht",
     ["Brezn Tasse", "Servus Shirt", "Brezn Geschenk minimalistisch"]),
    ("München", "Isarufer",
     "Eine sanft geschwungene Flusslinie mit einem kleinen Kiesstrand und einer einzelnen "
     "Welle, stark reduziert, freigestellt ohne Bebauung.",
     "Isarliebe", ["Gletscherblau", "Kieselgrau"],
     "Muenchner, die ihre Sommerabende an der Isar verbringen",
     "Wird im Fruehsommer als Geschenk fuer Freunde an der Isar gekauft",
     "Der Lieblingsort der Stadt statt der ueblichen Wahrzeichen",
     ["Isar Shirt", "Muenchen Sommer Geschenk", "Isarliebe Motiv"]),
    # ---------------------------------------------------------------- Dortmund
    ("Dortmund", "Das U",
     "Das grosse leuchtende U auf dem Dach des alten Brauereiturms als einfache Linienform mit "
     "drei kurzen Lichtstrahlen, freigestellt und reduziert.",
     "Mein Revier", ["Rostrot", "Anthrazit"],
     "Dortmunder, die stolz auf ihre Stadt im Revier sind",
     "Wird als Geschenk fuer den Freund gekauft, der nach Dortmund zieht",
     "Ein echtes Stadtwahrzeichen statt Anspielung auf Fussballfarben",
     ["Dortmund Shirt", "Ruhrgebiet Geschenk", "Revier Motiv"]),
    ("Dortmund", "Stahl und Kohle",
     "Ein Hochofen im Profil als klare geometrische Silhouette mit einer kleinen Rauchwolke, "
     "sehr reduziert und freigestellt.",
     "Aus dem Pott", ["Rostrot", "Grau"],
     "Menschen aus dem Ruhrgebiet mit Stolz auf die Industriegeschichte",
     "Wird als Hoodie fuer den Vater mit Bergbau- oder Stahlvergangenheit gekauft",
     "Industriestolz des Ruhrgebiets, der alle Revierstaedte verbindet",
     ["Ruhrpott Hoodie", "Pott Shirt", "Stahl Ruhrgebiet Geschenk"]),
    # ---------------------------------------------------------------- Gelsenkirchen
    ("Gelsenkirchen", "Glück auf",
     "Ein Foerderturm als schlichte Strebenkonstruktion mit zwei Seilscheiben, eine klare "
     "Linienzeichnung, freigestellt und mittig.",
     "Glück auf", ["Kohleschwarz", "Kupfer"],
     "Gelsenkirchener und Kumpel-Familien mit Bergbaugeschichte",
     "Wird als Geschenk zum runden Geburtstag des Opas aus dem Bergbau gekauft",
     "Der Bergmannsgruss als Heimatbekenntnis ohne Vereinsbezug",
     ["Glueck auf Shirt", "Foerderturm Motiv", "Bergbau Geschenk"]),
    ("Gelsenkirchen", "Grubenlampe",
     "Eine alte Grubenlampe als einfache Silhouette mit einer kleinen Flamme im Inneren, "
     "ruhig und freigestellt.",
     "Licht im Pott", ["Kupfer", "Schiefergrau"],
     "Menschen im Revier, die Traditionen des Bergbaus weitertragen",
     "Wird zur Barbarafeier am vierten Dezember als Geschenk gekauft",
     "Warmes Bergbausymbol statt lautem Stadionmotiv",
     ["Grubenlampe Motiv", "Bergbau Tradition Shirt", "Ruhrgebiet Tasse"]),
    # ---------------------------------------------------------------- Bremen
    ("Bremen", "Stadtmusikanten",
     "Esel, Hund, Katze und Hahn uebereinander gestapelt als eine einzige geschlossene "
     "Silhouette, sehr reduziert und freigestellt.",
     "", ["Backsteinrot", "Grau"],
     "Bremer und Maerchenfreunde mit Bezug zur Hansestadt",
     "Wird als Andenken nach einem Wochenende in Bremen gekauft",
     "Das weltbekannte Maerchenmotiv als moderne Minimalgrafik",
     ["Stadtmusikanten Shirt", "Bremen Geschenk", "Bremen Andenken"]),
    ("Bremen", "Roland",
     "Die Rolandfigur mit Schild und Schwert als schmale frontale Silhouette, stark vereinfacht "
     "und freigestellt.",
     "Moin", ["Sandstein", "Dunkelblau"],
     "Hanseaten, die Tradition und kurze Begruessungen moegen",
     "Wird als Geschenk zum Einzug in die erste Bremer Wohnung gekauft",
     "Hanseatische Gelassenheit in einem Wort und einer Figur",
     ["Moin Shirt", "Bremen Roland Motiv", "Norddeutsch Geschenk"]),
    # ---------------------------------------------------------------- Leipzig
    ("Leipzig", "Lerche",
     "Ein kleines rundes Gebaeck mit gekreuztem Teigstreifen auf dem Deckel, in einer ruhigen "
     "Linie gezeichnet, freigestellt.",
     "Leipzsch", ["Buttergelb", "Tannengruen"],
     "Leipziger mit Sinn fuer lokale Suessspeisen und Mundart",
     "Wird als Tasse fuer Kaffeeklatsch mit Freunden aus Sachsen gekauft",
     "Regionales Gebaeck mit Augenzwinkern statt Stadtwappen",
     ["Leipzig Tasse", "Leipziger Lerche", "Sachsen Geschenk"]),
    ("Leipzig", "Buchstadt",
     "Ein aufgeschlagenes Buch, aus dessen Seiten sich eine schmale Stadtsilhouette aus "
     "wenigen Linien erhebt, reduziert und freigestellt.",
     "Buchstadt", ["Tannengruen", "Creme"],
     "Leseratten und Studierende in Leipzig",
     "Wird zur Buchmesse im Fruehjahr als Andenken gekauft",
     "Die Messe- und Buchtradition der Stadt als Kulturmotiv",
     ["Leipzig Buchmesse Shirt", "Buch Motiv Leipzig", "Leseratte Geschenk"]),
    # ---------------------------------------------------------------- Istanbul
    ("Istanbul", "Bosporus",
     "Eine Haengebruecke ueber einer ruhigen Wasserlinie als eine durchgehende Linie, darueber "
     "eine einzelne Moewe, freigestellt.",
     "İstanbul", ["Tuerkis", "Sandbeige"],
     "Istanbuler und Tuerkei-Liebhaber in Deutschland",
     "Wird als Geschenk fuer Verwandte nach dem Sommerurlaub in Istanbul gekauft",
     "Das Stadtbild zwischen zwei Kontinenten in einer Linie",
     ["Istanbul Shirt", "Bosporus Motiv", "Tuerkei Geschenk"]),
    ("Istanbul", "Simit und Çay",
     "Ein runder Sesamkringel und ein tulpenfoermiges Teeglas nebeneinander, zwei ruhige Formen "
     "in gleicher Linienstaerke, freigestellt.",
     "Günaydın", ["Sesambraun", "Teerot"],
     "Menschen mit tuerkischen Wurzeln, die das Fruehstueck lieben",
     "Wird als Tasse fuer die Mutter mit Istanbuler Wurzeln verschenkt",
     "Das Istanbuler Fruehstuecksritual statt der ueblichen Moschee-Silhouette",
     ["Simit Cay Tasse", "Gunaydin Shirt", "Istanbul Fruehstueck Geschenk"]),
    ("Istanbul", "Galataturm",
     "Ein runder Steinturm mit spitzem Kegeldach als schmale Linienform, eine Moewe kreist "
     "darueber, stark reduziert.",
     "", ["Terrakotta", "Nachtblau"],
     "Architektur- und Reisefans mit Liebe zu Istanbul",
     "Wird nach einer Staedtereise nach Istanbul als Erinnerung gekauft",
     "Ein historischer Turm als ruhiges Reisemotiv",
     ["Istanbul Turm Motiv", "Istanbul Reise Shirt", "Tuerkei Andenken"]),
    # ---------------------------------------------------------------- Trabzon
    ("Trabzon", "Teehänge",
     "Sanfte Huegellinien mit angedeuteten Teereihen und einem kleinen Teeblatt darueber, "
     "sehr reduziert und freigestellt.",
     "Karadeniz", ["Teegruen", "Nebelgrau"],
     "Menschen von der Schwarzmeerkueste und ihre Kinder in Deutschland",
     "Wird als Geschenk fuer die Familie aus der Schwarzmeerregion gekauft",
     "Die gruene Teelandschaft als leises Heimatmotiv",
     ["Karadeniz Shirt", "Schwarzmeer Geschenk", "Tee Huegel Motiv"]),
    ("Trabzon", "Hamsi",
     "Ein einzelner kleiner Sardellenfisch als schlanke geschlossene Silhouette mit einer feinen "
     "Wellenlinie darunter, freigestellt.",
     "Hamsi", ["Silbergrau", "Meergruen"],
     "Schwarzmeer-Fans, fuer die Hamsi ein Stueck Heimat ist",
     "Wird als lustiges Geschenk fuer den Hamsi-Liebhaber in der Familie gekauft",
     "Regionaler Humor ueber das Lieblingsessen der Kueste",
     ["Hamsi Shirt", "Karadeniz Humor", "Trabzon Geschenk"]),
    # ---------------------------------------------------------------- Samsun
    ("Samsun", "Aufbruch 1919",
     "Ein kleines historisches Dampfschiff in reiner Seitenansicht auf einer schmalen Wellenlinie, "
     "sehr reduziert und freigestellt.",
     "", ["Anthrazit", "Sandbeige"],
     "Geschichtsinteressierte mit Wurzeln in Samsun und am Schwarzen Meer",
     "Wird zum neunzehnten Mai als Erinnerungsshirt gekauft",
     "Stadtgeschichte als ruhiges Symbol statt Fahnenmeer",
     ["Samsun Shirt", "19 Mayis Motiv", "Schwarzmeer Geschichte Geschenk"]),
    ("Samsun", "Küste",
     "Eine lange flache Kuestenlinie mit einem einzelnen Leuchtturm am Ende, eine durchgehende "
     "Linie, viel Freiraum.",
     "Samsun", ["Meeresblau", "Sandbeige"],
     "Samsuner in Deutschland mit Heimweh ans Meer",
     "Wird als Geschenk vor der Heimreise im Sommer gekauft",
     "Das Meer der Heimatstadt als stiller Sehnsuchtsort",
     ["Samsun Geschenk", "Leuchtturm Motiv", "Schwarzes Meer Shirt"]),
    # ---------------------------------------------------------------- Madrid
    ("Madrid", "Puerta de Alcalá",
     "Das klassische Stadttor mit drei Rundboegen als schlichte frontale Linienzeichnung, "
     "symmetrisch und freigestellt.",
     "Madrid", ["Terrakotta", "Creme"],
     "Spanien-Liebhaber und Madrider in Deutschland",
     "Wird nach einer Staedtereise nach Madrid als Andenken gekauft",
     "Das historische Stadttor als elegante Reisegrafik",
     ["Madrid Shirt", "Spanien Reise Geschenk", "Madrid Andenken"]),
    ("Madrid", "Chocolate con Churros",
     "Eine kleine Tasse heisse Schokolade mit zwei angelehnten Churros, ruhige klare Linien, "
     "mittig freigestellt.",
     "De Madrid al cielo", ["Schokobraun", "Zimt"],
     "Naschkatzen mit Liebe zu spanischer Kaffeehauskultur",
     "Wird als Tasse fuer die Freundin nach dem Madrid-Urlaub verschenkt",
     "Madrider Genuss mit einem alten Stadtspruch, der allen gehoert",
     ["Churros Tasse", "Madrid Spruch Shirt", "Spanien Tasse Geschenk"]),
    # ---------------------------------------------------------------- Barcelona
    ("Barcelona", "Sagrada Família",
     "Die schlanken Tuerme der Basilika als wenige feine senkrechte Linien mit kleinen Spitzen, "
     "sehr reduziert und freigestellt.",
     "", ["Sandstein", "Ocker"],
     "Architekturfans und Barcelona-Reisende",
     "Wird nach einer Reise nach Barcelona als Erinnerung gekauft",
     "Gaudis Baukunst als ruhige Linienzeichnung",
     ["Sagrada Familia Shirt", "Barcelona Reise Geschenk", "Gaudi Motiv"]),
    ("Barcelona", "Trencadís",
     "Eine kleine Eidechse aus wenigen unregelmaessigen Mosaikflaechen, als geschlossene Form "
     "freigestellt, sonst nichts.",
     "Bon dia", ["Keramikgruen", "Sonnengelb"],
     "Kunstliebhaber und Menschen mit katalanischen Wurzeln",
     "Wird als Sommershirt vor dem Urlaub in Katalonien gekauft",
     "Das Mosaik-Handwerk der Stadt als freundliche Grafik",
     ["Mosaik Eidechse Shirt", "Bon dia Motiv", "Katalonien Geschenk"]),
    # ---------------------------------------------------------------- Turin
    ("Turin", "Mole",
     "Die hohe schlanke Kuppel mit Spitze des Turiner Wahrzeichens als eine einzige Linie, "
     "darunter eine kurze Bergkette, freigestellt.",
     "Torino", ["Bordeaux", "Creme"],
     "Italien-Fans mit Liebe zu Turin und den Alpen",
     "Wird nach einer Reise ins Piemont als Andenken gekauft",
     "Das eigenwillige Stadtprofil mit Alpenblick statt Stadionbezug",
     ["Turin Shirt", "Torino Motiv", "Piemont Geschenk"]),
    ("Turin", "Bicerin",
     "Ein kleines Glas mit drei Schichten Kaffee, Schokolade und Sahne als klare Linienform, "
     "ruhig und freigestellt.",
     "Un bicerin, grazie", ["Espressobraun", "Creme"],
     "Kaffeeliebhaber mit Schwaeche fuer italienische Spezialitaeten",
     "Wird als Tasse fuer den Kaffeekenner im Freundeskreis verschenkt",
     "Die Turiner Kaffeespezialitaet als Insider-Motiv",
     ["Bicerin Tasse", "Italien Kaffee Geschenk", "Torino Kaffee"]),
    # ---------------------------------------------------------------- Mailand
    ("Mailand", "Duomo",
     "Die Fassade des Mailaender Doms als feine Linien mit vielen kleinen Spitzen, frontal, "
     "stark reduziert und freigestellt.",
     "Milano", ["Marmorgrau", "Gold"],
     "Mode- und Designfans mit Liebe zu Mailand",
     "Wird nach einem Wochenende in Mailand als Erinnerung gekauft",
     "Die gotische Dachlandschaft als elegante Modegrafik",
     ["Mailand Shirt", "Milano Motiv", "Italien Reise Geschenk"]),
    ("Mailand", "Tram",
     "Eine historische Strassenbahn in reiner Seitenansicht aus klaren Linien auf einer "
     "einzelnen Schiene, freigestellt.",
     "Andiamo", ["Orangegelb", "Anthrazit"],
     "Mailand-Liebhaber und Fans alter Strassenbahnen",
     "Wird als Geschenk fuer den Freund, der in Mailand studiert, gekauft",
     "Die alte Stadttram als sympathisches Alltagssymbol",
     ["Mailand Tram Motiv", "Andiamo Shirt", "Strassenbahn Geschenk"]),
    # ---------------------------------------------------------------- Paris
    ("Paris", "Métro",
     "Der geschwungene Jugendstil-Eingangsbogen einer Metrostation als eine einzige elegante "
     "Linie, zwei kleine Laternen, freigestellt.",
     "Paris", ["Flaschengruen", "Creme"],
     "Paris-Verliebte, die den Eiffelturm schon zu oft gesehen haben",
     "Wird nach der Staedtereise nach Paris als Erinnerung gekauft",
     "Ein weniger abgenutztes Pariser Symbol als der Eiffelturm",
     ["Paris Shirt minimalistisch", "Metro Paris Motiv", "Paris Geschenk"]),
    ("Paris", "Croissant",
     "Ein einzelnes Croissant aus einer durchgehenden Linie mit einem kleinen Kaffeefleck "
     "daneben, sehr ruhig und freigestellt.",
     "Bonjour tout le monde", ["Buttergelb", "Kaffeebraun"],
     "Fruehstuecksliebhaber mit Faible fuer Frankreich",
     "Wird als Tasse fuer die Kollegin mit Liebe zu Paris verschenkt",
     "Das Pariser Fruehstuecksgefuehl statt der ueblichen Wahrzeichen",
     ["Croissant Tasse", "Bonjour Shirt", "Frankreich Geschenk"]),
    # ---------------------------------------------------------------- Marseille
    ("Marseille", "Bonne Mère",
     "Die Basilika auf dem Felsen mit schlankem Glockenturm und kleiner Statue als eine Linie, "
     "darunter eine Welle, freigestellt.",
     "", ["Goldocker", "Terrakotta"],
     "Menschen aus Marseille und Freunde der Provence",
     "Wird nach dem Sommerurlaub an der Cote Bleue als Erinnerung gekauft",
     "Das Wahrzeichen ueber dem Hafen als ruhiges Heimatmotiv",
     ["Marseille Shirt", "Provence Geschenk", "Marseille Andenken"]),
    ("Marseille", "Pétanque",
     "Drei Metallkugeln und eine kleine Holzkugel auf einer kurzen Sandlinie, klare Kreise, "
     "stark reduziert und freigestellt.",
     "Tu tires ou tu pointes ?", ["Metallgrau", "Sandbeige"],
     "Boule-Spieler und Fans suedfranzoesischer Gelassenheit",
     "Wird als Geschenk fuer den Boule-Club zum Sommerfest gekauft",
     "Der typische Suedfrankreich-Spruch unter Boulespielern mit Humor",
     ["Petanque Shirt", "Boule Geschenk", "Marseille Humor"]),
    # ---------------------------------------------------------------- Neapel
    ("Neapel", "Vesuv & Espresso",
     "Der Vesuv als eine einzige Linie, aus dessen Krater Dampf wie aus einer kleinen "
     "Espressotasse aufsteigt, sehr reduziert und freigestellt.",
     "Napoli", ["Lavarot", "Espressobraun"],
     "Neapel-Liebhaber und Menschen mit sueditalienischen Wurzeln",
     "Wird als Tasse nach einer Reise an den Golf von Neapel verschenkt",
     "Vulkan und Kaffeekultur verbinden sich zu einem Stadtmotiv",
     ["Napoli Tasse", "Vesuv Motiv", "Neapel Geschenk"]),
    ("Neapel", "Pizza del Golfo",
     "Eine runde Pizza von oben, deren Belag als wenige Flaechen die Bucht mit dem Vulkan zeigt, "
     "reduziert und freigestellt.",
     "Vedi Napoli", ["Tomatenrot", "Basilikumgruen"],
     "Pizzaliebhaber und Fans neapolitanischer Lebensart",
     "Wird als Geschenk fuer den Pizzabaecker im Freundeskreis gekauft",
     "Die Geburtsstadt der Pizza mit einem Sprichwort, das allen gehoert",
     ["Pizza Napoli Shirt", "Neapel Pizza Geschenk", "Vedi Napoli Motiv"]),
    ("Neapel", "Cornetto",
     "Ein kleines gebogenes Horn als Gluecksbringer an einer duennen Kordel, geschlossene Form, "
     "ruhig und freigestellt.",
     "Porta fortuna", ["Korallenrot", "Gold"],
     "Aberglaeubische Italienfans und Freunde neapolitanischer Traditionen",
     "Wird als Gluecksbringer vor einer Pruefung oder einem Neustart verschenkt",
     "Der neapolitanische Gluecksbringer als modernes Minimalmotiv",
     ["Cornetto Gluecksbringer", "Porta fortuna Shirt", "Italien Glueck Geschenk"]),
]


def eintraege() -> list[trends.Trend]:
    liste = []
    for stadt, thema, motiv, spruch, farben, ziel, kauf, winkel, begriffe in _IDEEN:
        liste.append(trends.Trend(
            thema=f"{stadt}: {thema}", kategorie=KATEGORIE, warum=_WARUM, zeitraum=_ZEITRAUM,
            zielgruppe=ziel, kaufmoment=kauf, verkaufswinkel=winkel, motiv=motiv,
            stil=_STIL, farben=list(farben), spruch=spruch, produkt=_PRODUKT,
            druckhinweis=_DRUCK, risiko=_RISIKO, suchbegriffe=list(begriffe), quellen=[]))
    return liste


def lade(db: Any, *, filter_check: Callable[[str], Any] | None = None,
         heute: date | None = None) -> dict:
    """Kollektion als Empfehlungen ablegen. Kostenlos; mehrfaches Laden verdoppelt nichts."""
    bericht = trends.speichere(
        db, eintraege(), belege=[], angebote={},
        filter_check=filter_check or trends._schutzfilter_check,
        heute=heute or date.today(), erwartete_kategorie=KATEGORIE)
    bericht["kosten_usd"] = 0.0
    return bericht
