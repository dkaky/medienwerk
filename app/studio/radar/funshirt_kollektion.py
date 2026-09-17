"""Fun-Shirt-Sprueche: kurze, witzige deutsche Sprueche mit einem kleinen passenden Motiv.

Vorgabe des Betreibers (17.09.2026): Der Shop fokussiert sich auf GENAU diese
Nische. Jeder Eintrag ist Spruch zuerst, das Motiv bleibt klein und dient dem
Spruch, statt ihn zu erschlagen - typischer Fun-Shirt-Stil.

Die Eintraege laufen durch dieselbe Qualitaets- und Rechtepruefung wie die
Websuche (``trends.speichere``): keine Marken, keine Zitate, kein Copy-Paste.
Laden kostet nichts; Geld kostet erst "Motiv erzeugen".
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from app.studio.radar import trends

KATEGORIE = "funshirt"

_STIL = ("Minimalistischer realistischer Siebdruck-Look: der Spruch in einer klaren, "
         "gut lesbaren Handschrift- oder Blockschrift, das Motiv klein daneben oder "
         "darunter platziert und dem Spruch klar untergeordnet, viel Freiraum, "
         "hoechstens zwei Druckfarben")
_DRUCK = ("Spruch und Motiv als eine geschlossene freigestellte Einheit mit kraeftigen "
          "Konturen, keine feinen Verlaeufe, auf hellen und dunklen Textilien lesbar")
_RISIKO = "Vor dem Einstellen pruefen, dass der Spruch nicht mit einem bekannten Zitat kollidiert"
_ZEITRAUM = "Immergruen, Spitzen zu Geburtstagen, Feiertagen und als Spontankauf"
_WARUM = ("Fun-Shirts mit Spruch sind ein dauerhaft gefragtes Geschenksegment; ein kleines "
          "eigenes Motiv macht den Spruch zum Blickfang statt zum reinen Textdruck.")
_PRODUKT = "T-Shirt, alternativ Hoodie oder Tasse"

# (thema, motiv, spruch, farben, zielgruppe, kaufmoment, verkaufswinkel, suchbegriffe)
_IDEEN: list[tuple[str, ...]] = [
    ("Kaffee zuerst",
     "Eine einzelne dampfende Kaffeetasse mit einem winzigen Wecker daneben, klare Linien, "
     "klein unter dem Schriftzug platziert, freigestellt.",
     "Erst Kaffee. Dann reden.", ["Kaffeebraun", "Creme"],
     "Morgenmuffel, die vor dem ersten Kaffee nicht ansprechbar sind",
     "Wird spontan online gekauft, wenn jemand sich im Freundeskreis wiedererkennt",
     "Der ehrliche Morgenmoment statt des ueblichen Kaffee-Klischees",
     ["Kaffee Spruch Shirt", "Fun Shirt Kaffee", "Lustiges Geschenk Buero"]),
    ("Couch-Marathon",
     "Eine kleine Fernbedienung neben einer gemuetlichen Wolldecke, beides als einfache "
     "Umrisse, sehr klein unter dem Schriftzug, freigestellt.",
     "Profi im Nichtstun", ["Anthrazit", "Senfgelb"],
     "Serienfans, die das Wochenende bewusst faul verbringen",
     "Wird als Geschenk zum Einzug in die erste eigene Wohnung gekauft",
     "Stolz aufs Nichtstun statt der ueblichen Motivations-Sprueche",
     ["Fun Shirt Couch", "Lustiger Spruch Serie", "Geschenk Serienjunkie"]),
    ("Montags-Diagnose",
     "Ein kleines Thermometer mit knickendem Ausschlag, minimal gezeichnet, unter dem "
     "Schriftzug platziert, freigestellt.",
     "Montags fieberfrei, sonst nichts.", ["Warngrau", "Signalrot"],
     "Arbeitnehmer mit trockenem Humor ueber den Wochenstart",
     "Wird als Geschenk fuer Kollegen zum Bueroauszug gekauft",
     "Bueroalltag mit Selbstironie statt allgemeinem Motivationsspruch",
     ["Montag Spruch Shirt", "Buero Humor Geschenk", "Fun Shirt Arbeit"]),
    ("Datenbank im Kopf",
     "Ein winziges Festplattensymbol mit einem kleinen Ladebalken darunter, reduziert und "
     "freigestellt, klein unter dem Schriftzug.",
     "404 - Motivation nicht gefunden", ["Codeblau", "Weiss"],
     "IT-Angestellte und Studierende mit Nerd-Humor",
     "Wird als Geschenk fuer den Informatik-Kollegen zum Geburtstag gekauft",
     "Insider-Humor der IT-Branche statt allgemeiner Buerosprueche",
     ["Nerd Shirt Spruch", "IT Humor Geschenk", "404 Shirt"]),
    ("Wochenend-Modus",
     "Ein kleiner Schalter in Position 'An', schlicht gezeichnet, unter dem Schriftzug, "
     "freigestellt.",
     "Wochenend-Modus: aktiviert", ["Mint", "Anthrazit"],
     "Berufstaetige, die den Freitagabend feiern",
     "Wird als spontaner Kauf am Freitag vor dem Wochenende getaetigt",
     "Die Vorfreude aufs Wochenende als kleiner visueller Schalter",
     ["Wochenende Spruch Shirt", "Freitag Fun Shirt", "Lustiges Wochenende Geschenk"]),
    ("Chaos-Organisation",
     "Ein winziger Zettelstapel mit einer schiefen Büroklammer, minimal gezeichnet, unter dem "
     "Schriftzug, freigestellt.",
     "Ich hab ein System. Es heißt Chaos.", ["Senfgelb", "Anthrazit"],
     "Chaotische Organisationstalente mit Selbstironie",
     "Wird als Geschenk fuer die chaotische beste Freundin gekauft",
     "Ehrliche Selbstironie statt gestylter Motivationssprueche",
     ["Chaos Spruch Shirt", "Lustiges Geschenk Freundin", "Fun Shirt Buero Chaos"]),
    ("Akku leer",
     "Ein kleines Batteriesymbol mit fast leerem Balken, schlicht, unter dem Schriftzug "
     "platziert, freigestellt.",
     "Akkustand: 3 Prozent", ["Warnrot", "Grau"],
     "Erschoepfte Eltern und Berufstaetige mit Humor",
     "Wird als Geschenk fuer frischgebackene Eltern nach der Geburt gekauft",
     "Der ehrliche Erschoepfungszustand als Insider-Scherz",
     ["Erschoepft Spruch Shirt", "Fun Shirt Eltern", "Lustiges Geschenk Muede"]),
    ("Hundehaare inklusive",
     "Eine winzige Pfote neben einem kleinen Flusenroller, reduziert gezeichnet, unter dem "
     "Schriftzug, freigestellt.",
     "Kommt mit Hundehaaren. Gratis.", ["Sandbeige", "Kastanienbraun"],
     "Hundebesitzer mit Humor ueber Hundehaare auf jedem Kleidungsstueck",
     "Wird als Geschenk fuer den frisch gebackenen Hundebesitzer gekauft",
     "Der Alltag mit Hund als liebevoller Insider-Scherz",
     ["Hunde Spruch Shirt", "Fun Shirt Hundebesitzer", "Lustiges Geschenk Hund"]),
    ("Katzenpersonal",
     "Eine winzige Katzenpfote auf einer kleinen Krone, minimal gezeichnet, unter dem "
     "Schriftzug, freigestellt.",
     "Angestellt bei meiner Katze", ["Kohlegrau", "Gold"],
     "Katzenbesitzer, die sich als Personal ihrer Katze sehen",
     "Wird als Geschenk zum Einzug einer neuen Katze gekauft",
     "Das bekannte Katzenbesitzer-Gefuehl als kleines Symbol",
     ["Katzen Spruch Shirt", "Fun Shirt Katzenbesitzer", "Lustiges Geschenk Katze"]),
    ("Pflanzenmama",
     "Ein winziges Blatt mit einer kleinen Giesskanne daneben, reduziert, unter dem Schriftzug, "
     "freigestellt.",
     "Pflanzenmama mit 47 Kindern", ["Salbeigruen", "Terrakotta"],
     "Zimmerpflanzen-Sammlerinnen mit vielen Pflanzen",
     "Wird als Geschenk fuer die Freundin mit der Pflanzensammlung gekauft",
     "Das Pflanzen-Hobby als liebevoller Insider-Scherz",
     ["Pflanzen Spruch Shirt", "Fun Shirt Pflanzenmama", "Geschenk Pflanzenliebhaberin"]),
    ("Gartenchef",
     "Eine kleine Harke neben einer winzigen Tomate, schlicht gezeichnet, unter dem "
     "Schriftzug, freigestellt.",
     "Chef im Garten, Praktikant im Haus", ["Tomatenrot", "Laubgruen"],
     "Hobbygaertner mit Humor ueber die eigenen Prioritaeten",
     "Wird als Geschenk zum Vatertag fuer den Hobbygaertner gekauft",
     "Die Gartenleidenschaft mit einem Augenzwinkern auf den Haushalt",
     ["Garten Spruch Shirt", "Fun Shirt Vatertag", "Lustiges Geschenk Hobbygaertner"]),
    ("Sparfuchs-Modus",
     "Ein winziges Sparschwein mit einem kleinen Fragezeichen darueber, reduziert, unter dem "
     "Schriftzug, freigestellt.",
     "Sparfuchs mit Luecken im Plan", ["Roségold", "Anthrazit"],
     "Menschen mit Humor ueber die eigenen Spartraeume",
     "Wird als Geschenk fuer den sparsamen, aber grosszuegigen Freund gekauft",
     "Selbstironie ueber das eigene Sparverhalten statt Finanztipps",
     ["Spar Spruch Shirt", "Fun Shirt Geld", "Lustiges Geschenk Sparfuchs"]),
    ("Bücherstapel-Notstand",
     "Ein kleiner schiefer Bücherstapel mit einem winzigen Lesezeichen, reduziert, unter dem "
     "Schriftzug, freigestellt.",
     "Zu viele Bücher. Zu wenig Zeit.", ["Tannengruen", "Creme"],
     "Leseratten mit ungelesenem Bücherstapel zu Hause",
     "Wird als Geschenk fuer die buecherverliebte Freundin gekauft",
     "Das bekannte Leseratten-Dilemma als kleines Symbol",
     ["Buecher Spruch Shirt", "Fun Shirt Leseratte", "Geschenk Buecherwurm"]),
    ("Laufmuffel ehrlich",
     "Ein winziger Turnschuh, der still auf der Seite liegt, minimal gezeichnet, unter dem "
     "Schriftzug, freigestellt.",
     "Sport ist, wenn ich zuschaue.", ["Sportgruen", "Weiss"],
     "Menschen mit Humor ueber ihre fehlende Sportmotivation",
     "Wird als lustiges Geschenk fuer den sportfaulen Freund gekauft",
     "Ehrliche Anti-Motivation statt des ueblichen Fitness-Klischees",
     ["Sport Spruch Shirt lustig", "Fun Shirt Faulheit", "Lustiges Geschenk Sportmuffel"]),
    ("Nudel-Notfall",
     "Eine kleine Nudelgabel mit einer einzelnen aufgerollten Nudel, schlicht gezeichnet, "
     "unter dem Schriftzug, freigestellt.",
     "Nudeln lösen die meisten Probleme", ["Eigelb", "Tomatenrot"],
     "Nudelliebhaber mit Humor ueber ihr Lieblingsessen",
     "Wird als Geschenk fuer den besten Freund mit Nudel-Vorliebe gekauft",
     "Alltagshumor rund ums Lieblingsessen statt allgemeiner Kochsprueche",
     ["Nudel Spruch Shirt", "Fun Shirt Essen", "Lustiges Geschenk Foodie"]),
    ("Wein-Wissenschaft",
     "Ein winziges Weinglas mit einem kleinen Messbecher-Strich, reduziert, unter dem "
     "Schriftzug, freigestellt.",
     "Wein ist Wissenschaft. Ich forsche viel.", ["Weinrot", "Creme"],
     "Weinliebhaber mit Humor ueber ihr Hobby",
     "Wird als Geschenk zum Geburtstag der Freundin mit Weinvorliebe gekauft",
     "Selbstironischer Genuss statt ernster Wein-Expertise",
     ["Wein Spruch Shirt", "Fun Shirt Wein", "Lustiges Geschenk Weinliebhaberin"]),
    ("Bett-Gravitation",
     "Ein winziges Kissen mit gekruemmten Bewegungslinien darum, minimal gezeichnet, unter "
     "dem Schriftzug, freigestellt.",
     "Mein Bett hat eine starke Anziehungskraft", ["Nachtblau", "Creme"],
     "Langschlaefer mit Humor ueber ihr Verhaeltnis zum Bett",
     "Wird als Geschenk fuer den notorischen Langschlaefer im Haushalt gekauft",
     "Der ewige Kampf gegen den Wecker als kleines Bild",
     ["Schlaf Spruch Shirt", "Fun Shirt Langschlaefer", "Lustiges Geschenk Bett"]),
    ("Werkzeugkasten-Ehe",
     "Ein winziger Schraubenschluessel neben einem kleinen Herz, schlicht gezeichnet, unter "
     "dem Schriftzug, freigestellt.",
     "Reparier ich schon irgendwie", ["Werkstattgrau", "Warnorange"],
     "Heimwerker mit trockenem Humor ueber ihre Reparaturversuche",
     "Wird als Geschenk zum Vatertag fuer den Hobby-Heimwerker gekauft",
     "Selbstironie ueber Heimwerker-Ehrgeiz statt Profi-Werbung",
     ["Heimwerker Spruch Shirt", "Fun Shirt Vatertag Werkzeug", "Lustiges Geschenk Handwerker"]),
    ("Serien-Marathon-Trophäe",
     "Ein winziger Pokal mit einem kleinen Play-Symbol darin, reduziert, unter dem Schriftzug, "
     "freigestellt.",
     "Serien-Weltmeister im Wohnzimmer", ["Goldgelb", "Anthrazit"],
     "Serienfans, die ganze Staffeln an einem Wochenende schauen",
     "Wird als Scherzgeschenk nach einem gemeinsamen Serien-Wochenende gekauft",
     "Der Bingewatching-Stolz als humorvolle Auszeichnung",
     ["Serien Spruch Shirt", "Fun Shirt Serienfan", "Lustiges Geschenk Netflix"]),
    ("Kopfhörer-Rückzug",
     "Ein winziger Kopfhoerer-Buegel mit einem kleinen Notensymbol daneben, schlicht gezeichnet, "
     "unter dem Schriftzug platziert, freigestellt.",
     "Kopfhörer drin, Welt draußen", ["Schiefergrau", "Neongruen"],
     "Introvertierte, die sich mit Musik von der Welt abschirmen",
     "Wird als Geschenk fuer den introvertierten Kollegen im Grossraumbuero gekauft",
     "Introvertiertes Lebensgefuehl als klarer, sympathischer Spruch",
     ["Introvertiert Spruch Shirt", "Fun Shirt Kopfhoerer", "Lustiges Geschenk introvertiert"]),
    ("Süßigkeiten-Ausrede",
     "Ein winziges Bonbon mit einem gedrehten Papierende, minimal gezeichnet, unter dem "
     "Schriftzug, freigestellt.",
     "Süßes zählt bei mir immer.", ["Zuckerrosa", "Minzgruen"],
     "Naschkatzen mit Humor ueber ihre Suessigkeiten-Liebe",
     "Wird als Geschenk fuer die naschende beste Freundin gekauft",
     "Ehrliches Naschverhalten statt schlechtem Gewissen",
     ["Suess Spruch Shirt", "Fun Shirt Naschkatze", "Lustiges Geschenk Suessigkeiten"]),
    ("Pflanzen-Talk",
     "Ein winziges Blatt mit kleinen Sprechblasen-Punkten daneben, reduziert, unter dem "
     "Schriftzug, freigestellt.",
     "Ich rede mit meinen Pflanzen. Sie hören zu.", ["Farngruen", "Creme"],
     "Pflanzenliebhaber mit Humor ueber ihr Gespraech mit Zimmerpflanzen",
     "Wird als Geschenk zum Einzug in die erste eigene Wohnung gekauft",
     "Das liebevoll schrullige Pflanzenritual als kleiner Scherz",
     ["Pflanzen Spruch Shirt lustig", "Fun Shirt Zimmerpflanzen", "Geschenk Pflanzenfreund"]),
    ("Backofen-Optimismus",
     "Ein winziges Cupcake-Symbol mit einer kleinen schiefen Kerze, reduziert, unter dem "
     "Schriftzug, freigestellt.",
     "Sieht schlimmer aus, als es schmeckt", ["Buttergelb", "Zuckerrosa"],
     "Hobbybaecker mit Humor ueber misslungene Backversuche",
     "Wird als Geschenk fuer die backfreudige, aber chaotische Freundin gekauft",
     "Ehrlicher Umgang mit misslungenem Gebaeck statt Perfektionsdruck",
     ["Backen Spruch Shirt", "Fun Shirt Hobbybaecker", "Lustiges Geschenk Backen"]),
    ("Wecker-Feindschaft",
     "Ein winziger Wecker mit einem kleinen Blitz-Symbol daneben, schlicht gezeichnet, unter "
     "dem Schriftzug, freigestellt.",
     "Mein Wecker und ich sind nicht befreundet", ["Alarmrot", "Anthrazit"],
     "Menschen, die morgens den Wecker mehrfach verschieben",
     "Wird als Scherzgeschenk fuer den notorischen Schlummertaste-Drücker gekauft",
     "Der taegliche Morgenkampf als humorvolles Alltagsmotiv",
     ["Wecker Spruch Shirt", "Fun Shirt Morgenmuffel", "Lustiges Geschenk Aufstehen"]),
    ("Emoji-Übersetzer",
     "Ein winziges Sprechblasen-Symbol mit einem kleinen Fragezeichen, reduziert, unter dem "
     "Schriftzug, freigestellt.",
     "Ich versteh nur Bahnhof und Emojis", ["Himmelblau", "Sonnengelb"],
     "Digitale Generation mit Humor ueber ihre Kommunikation",
     "Wird als Geschenk unter Freunden zum Spass verschickt",
     "Moderne Alltagskommunikation der juengeren Generation mit Augenzwinkern",
     ["Emoji Spruch Shirt", "Fun Shirt Kommunikation", "Lustiges Geschenk Jugendwort"]),
    ("Kabelsalat-Champion",
     "Ein winziges verschlungenes Kabel, schlicht als Linie gezeichnet, unter dem Schriftzug, "
     "freigestellt.",
     "Kabel für alles. Finde nie das richtige.", ["Steckdosengrau", "Neongelb"],
     "Technikbesitzer mit Humor ueber ihre Kabelsammlung",
     "Wird als Geschenk fuer den Technik-Nerd mit Kabelschublade gekauft",
     "Der Alltagsfrust mit Technik als sympathischer Insider-Scherz",
     ["Technik Spruch Shirt", "Fun Shirt Kabelsalat", "Lustiges Geschenk Nerd"]),
    ("Käse-Bekenntnis",
     "Ein winziges Käsestück mit typischen Löchern, minimal gezeichnet, unter dem Schriftzug, "
     "freigestellt.",
     "Käse ist die Antwort. Was war die Frage?", ["Käsegelb", "Kastanienbraun"],
     "Käseliebhaber mit Humor ueber ihre Vorliebe",
     "Wird als Geschenk fuer den kaesevernarrten Mitbewohner gekauft",
     "Bedingungslose Käseliebe als kurzer, klarer Scherz",
     ["Kaese Spruch Shirt", "Fun Shirt Kaese", "Lustiges Geschenk Foodie Kaese"]),
    ("Plan-Chaos",
     "Ein winziger Kalender mit einem kleinen durchgestrichenen Termin, reduziert, unter dem "
     "Schriftzug, freigestellt.",
     "Ich hab einen Plan. Er ändert sich ständig.", ["Kalenderblau", "Warnorange"],
     "Spontane Menschen mit Humor ueber ihre schlechte Planung",
     "Wird als Geschenk fuer den chaotischen, aber liebenswerten Freund gekauft",
     "Ehrliche Selbstironie ueber Planlosigkeit statt Zeitmanagement-Tipps",
     ["Plan Spruch Shirt", "Fun Shirt Chaos Planung", "Lustiges Geschenk Spontan"]),
    ("Regentag-Realismus",
     "Ein winziger Regenschirm mit drei kleinen Regentropfen, schlicht gezeichnet, unter dem "
     "Schriftzug, freigestellt.",
     "Sonnenschein im Herzen, Regen auf dem Kopf", ["Regengrau", "Sonnengelb"],
     "Menschen mit Humor ueber schlechtes Wetter und gute Laune",
     "Wird als aufmunterndes Geschenk an verregneten Tagen gekauft",
     "Gute-Laune-Spruch mit Wetterbezug statt generischer Motivation",
     ["Regen Spruch Shirt", "Fun Shirt Wetter", "Lustiges Geschenk gute Laune"]),
    ("Team Nachtschicht",
     "Ein winziger Halbmond mit einer kleinen Tasse daneben, minimal gezeichnet, unter dem "
     "Schriftzug, freigestellt.",
     "Team Nachteule seit Geburt", ["Nachtblau", "Silbergrau"],
     "Nachtaktive Menschen, die abends erst richtig wach werden",
     "Wird als Geschenk fuer den notorischen Nachtmenschen im Freundeskreis gekauft",
     "Das Nachtmensch-Gefuehl als eigenstaendiges Lebensgefuehl-Motiv",
     ["Nachteule Spruch Shirt", "Fun Shirt Nachtmensch", "Lustiges Geschenk Nachtaktiv"]),
    ("Einkaufszettel-Vergessen",
     "Ein winziger Einkaufszettel mit einem kleinen Kreuz auf einer Zeile, reduziert, unter "
     "dem Schriftzug, freigestellt.",
     "Zettel geschrieben. Zettel vergessen.", ["Papierbeige", "Anthrazit"],
     "Vergessliche Menschen mit Humor ueber ihre Alltagspannen",
     "Wird als liebevolles Scherzgeschenk fuer die vergessliche Oma gekauft",
     "Alltagsvergesslichkeit als warmherziger Scherz in der ganzen Familie",
     ["Vergesslich Spruch Shirt", "Fun Shirt Alltag", "Lustiges Geschenk vergesslich"]),
    ("Bergwanderer-Ehrlichkeit",
     "Ein winziger Berggipfel mit einer kleinen Fahne, schlicht gezeichnet, unter dem "
     "Schriftzug, freigestellt.",
     "Bergauf für die Aussicht, bergab für die Wurst", ["Bergblau", "Erdbraun"],
     "Wanderfreunde mit Humor ueber ihre wahren Beweggruende",
     "Wird als Geschenk fuer den Wanderfreund vor der naechsten Bergtour gekauft",
     "Ehrlicher Wander-Humor statt reiner Naturromantik",
     ["Wandern Spruch Shirt", "Fun Shirt Bergtour", "Lustiges Geschenk Wanderfreund"]),
]


def eintraege() -> list[trends.Trend]:
    liste = []
    for thema, motiv, spruch, farben, ziel, kauf, winkel, begriffe in _IDEEN:
        liste.append(trends.Trend(
            thema=thema, kategorie=KATEGORIE, warum=_WARUM, zeitraum=_ZEITRAUM,
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
