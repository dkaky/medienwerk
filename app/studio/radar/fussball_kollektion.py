"""Fussball-Kollektion: feste, durchdachte Motivideen - markenfrei, minimalistisch.

Vorgabe des Betreibers (16.09.2026): Kategorie "Fussball" statt "Sport", sehr
minimalistische Motive, ab und zu ein Spruch, kreativ.

**Bewusst ohne Vereine und Ligen.** Vereinsnamen, Wappen, Vereinsfarben in
Wiedererkennungsform, Vereinssprueche ("Mia san mia" ist eine eingetragene Marke)
und Ligennamen sind geschuetzt. Auf verkaufter Ware ist das Markenverletzung,
eBay loescht solche Angebote auf Meldung der Rechteinhaber. Die Kollektion lebt
deshalb von dem, was allen Fans gehoert: Rituale, Positionen, Stadion, Spielzeit,
Taktik - und von der Fussballkultur der sechs grossen Fussballlaender ueber
Sprache und allgemeine Begriffe (Catenaccio, Tiki-Taka, Petit Pont ...).

Die Eintraege laufen durch dieselbe Qualitaets- und Rechtepruefung wie die
Websuche (``trends.speichere``). Laden kostet nichts; Geld kostet erst
"Motiv erzeugen".
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from app.studio.radar import trends

KATEGORIE = "fussball"

_STIL = ("Minimalistische realistische Siebdruck-Illustration mit klaren Formen, "
         "viel Freiraum und hoechstens zwei Druckfarben")
_DRUCK = ("Eine geschlossene freigestellte Form mit kraeftigen Linien, keine feinen "
          "Verlaeufe, auf hellen und dunklen Textilien lesbar")
_RISIKO = ("Vor dem Einstellen pruefen, dass keine Vereinsfarben-Kombination oder "
           "Vereinsschrift anklingt")
_ZEITRAUM = "Immergruen, Spitzen zum Saisonstart im August und zu grossen Turnieren"
_WARUM = ("Fussball ist ganzjaehrig das groesste Fan-Thema in Deutschland und Europa; "
          "vereinsfreie Motive verkaufen sich an Fans aller Clubs.")
_PRODUKT = "T-Shirt, alternativ Hoodie oder Tasse"

# (thema, motiv, spruch, farben, zielgruppe, kaufmoment, verkaufswinkel, suchbegriffe)
_IDEEN: list[tuple[str, str, str, list[str], str, str, str, list[str]]] = [
    # ---------------------------------------------------------- Spielfeld & Spielzeit
    ("Anstosskreis",
     "Eine einzelne duenne Mittellinie mit Anstosskreis und Anstosspunkt, aus einer durchgehenden "
     "Linie gezeichnet, mittig freigestellt, sonst nichts.",
     "90 Minuten. Mindestens.", ["Weiss", "Rasengruen"],
     "Fussballfans, die jedes Wochenende im Stadion stehen",
     "Wird als Geschenk zum Geburtstag eines treuen Stadiongaengers gekauft",
     "Das Spielfeld selbst als reduziertes Zeichen statt Ball und Pokal",
     ["Fussball Shirt minimalistisch", "Fussball Geschenk Mann", "Anstosskreis Motiv"]),
    ("Nachspielzeit",
     "Eine digitale Anzeigetafel als schlichtes Rechteck mit leuchtender grosser Zahl plus vier, "
     "klar umrandet, ohne Stadion und ohne weitere Elemente.",
     "Nachspielzeit", ["Bernstein", "Anthrazit"],
     "Fans, die spaete Tore und Drama in der Schlussphase lieben",
     "Wird nach einem knappen Last-Minute-Sieg als Erinnerung gekauft",
     "Der Moment der Nachspielzeit als Symbol fuer Hoffnung bis zuletzt",
     ["Nachspielzeit Shirt", "Fussball Spruch Shirt", "Last Minute Tor Geschenk"]),
    ("Elfmeterpunkt",
     "Ein einzelner weisser Punkt mit einem angedeuteten Fussball daneben, von oben gesehen, "
     "umgeben von einem schmalen Kreisbogen des Strafraums, stark reduziert.",
     "Nerven wie Drahtseile", ["Weiss", "Tiefgruen"],
     "Spieler und Fans, die Elfmeterschiessen nicht ansehen koennen",
     "Wird als Geschenk fuer den sicheren Elfmeterschuetzen im Team gekauft",
     "Druck und Ruhe in einem einzigen Punkt statt eines ganzen Spielfelds",
     ["Elfmeter Shirt", "Fussball Geschenk Spieler", "Strafstoss Motiv"]),
    ("Flutlicht",
     "Vier schlanke Flutlichtmasten als geometrische Striche mit leuchtenden Rasterkoepfen, "
     "symmetrisch angeordnet, darunter eine schmale Linie als Rasenkante.",
     "", ["Warmweiss", "Nachtblau"],
     "Fans, die Abendspiele unter Flutlicht am meisten lieben",
     "Wird vor dem ersten Flutlichtspiel der Saison fuer die Stadiontour gekauft",
     "Die Stimmung eines Abendspiels ohne ein einziges Vereinsmerkmal",
     ["Flutlicht Shirt", "Stadion Motiv minimalistisch", "Fussball Abendspiel Geschenk"]),
    ("Sonntag ist Spieltag",
     "Ein schlichter Fussball aus einer einzigen durchgehenden Linie gezeichnet, darunter eine "
     "kurze waagerechte Linie wie ein Rasenstreifen, viel Freiraum.",
     "Sonntag ist Spieltag", ["Schwarz", "Rasengruen"],
     "Amateurkicker und Eltern, die sonntags am Spielfeldrand stehen",
     "Wird als Geschenk fuer den Trainer der Jugendmannschaft zum Saisonende gekauft",
     "Der Amateursonntag statt Profi-Glamour trifft die breite Basis",
     ["Sonntag Spieltag Shirt", "Kreisliga Geschenk", "Fussball Papa Shirt"]),
    ("Kreisliga-Legende",
     "Ein abgetretener Fussballschuh im Profil mit einem kleinen Stern darueber, kraeftige "
     "Kontur, freigestellt und leicht gealtert wirkend.",
     "Kreisliga-Legende", ["Kreideweiss", "Dunkelgruen"],
     "Amateurspieler ueber dreissig, die seit Jahren im Verein kicken",
     "Wird von der Mannschaft zum Abschied oder runden Geburtstag verschenkt",
     "Humorvolle Ehre fuer den Amateur statt Nachahmung von Profis",
     ["Kreisliga Shirt", "Kreisliga Legende Geschenk", "Fussball Verein Abschied Geschenk"]),
    ("Zu null",
     "Ein Paar reduzierte Torwarthandschuhe als geschlossene Umrisse, die Handflaechen zueinander "
     "gedreht, mittig freigestellt, ohne Hintergrund.",
     "Zu null.", ["Neongelb", "Schwarz"],
     "Torhueter aller Altersklassen und ihre stolzen Familien",
     "Wird nach dem ersten Spiel ohne Gegentor als Belohnung gekauft",
     "Die Torwartseele als eigene Nische statt allgemeiner Fussballmotive",
     ["Torwart Shirt", "Torwart Geschenk", "Zu null Spruch"]),
    ("Nummer 10",
     "Eine grosse freigestellte Zahl 10 in kantiger Retro-Sportschrift mit schmaler Aussenlinie, "
     "leicht verwittert wie ein alter Rueckenaufdruck.",
     "", ["Creme", "Bordeaux"],
     "Kreative Mittelfeldspieler und Fans der klassischen Spielmacherrolle",
     "Wird als Geburtstagsgeschenk fuer den Spielmacher im Freundeskreis gekauft",
     "Die Rolle der Zehn als Mythos statt eines bestimmten Spielers",
     ["Nummer 10 Shirt", "Spielmacher Geschenk", "Retro Fussball Shirt"]),
    ("Taktiktafel",
     "Eine kleine Taktiktafel mit Kreidekreuzen, Kreisen und einem geschwungenen Pfeil zum Tor, "
     "handgezeichnet wirkend, stark reduziert und freigestellt.",
     "Der Plan steht.", ["Kreideweiss", "Tafelgruen"],
     "Trainer und Taktikliebhaber, die jedes Spiel analysieren",
     "Wird der Trainerin oder dem Trainer zum Saisonabschluss geschenkt",
     "Die Liebe zur Taktik ist eine eigene Fan-Nische mit wenig Konkurrenz",
     ["Trainer Geschenk Fussball", "Taktik Shirt", "Fussballtrainer Tasse"]),
    ("Pfiff",
     "Eine silberne Schiedsrichterpfeife an einer einfachen Kordel, seitlich gesehen, mit drei "
     "kurzen Schallstrichen, klar konturiert und freigestellt.",
     "Weiterspielen!", ["Silbergrau", "Schwarz"],
     "Amateur-Schiedsrichter und Menschen, die gern das letzte Wort haben",
     "Wird zum bestandenen Schiedsrichterlehrgang als Geschenk gekauft",
     "Die oft vergessene Rolle des Schiedsrichters mit Augenzwinkern",
     ["Schiedsrichter Geschenk", "Schiri Shirt", "Pfeife Spruch Shirt"]),
    ("Grätsche",
     "Eine abstrakte Silhouette einer sauberen Grätsche aus drei dynamischen Pinselstrichen, "
     "mit Ball, ohne Gesicht und ohne Trikotdetails, freigestellt.",
     "Mit Gefühl.", ["Tiefschwarz", "Signalrot"],
     "Verteidiger, die fuer das Team jeden Zweikampf annehmen",
     "Wird dem Abwehrchef der Freizeitmannschaft zum Geburtstag geschenkt",
     "Ehrliche Defensivarbeit als Stolz statt Glanz der Stuermer",
     ["Verteidiger Shirt", "Graetsche Spruch", "Fussball Abwehr Geschenk"]),
    ("Auswärtsfahrt",
     "Ein kleiner Reisebus in reiner Seitenansicht aus klaren Linien, ein Schal weht aus dem "
     "Fenster, ohne Schrift und Farben eines Vereins.",
     "Auswärts ist Heimat.", ["Senfgelb", "Dunkelblau"],
     "Fans, die ihre Mannschaft zu jedem Auswaertsspiel begleiten",
     "Wird vor der ersten gemeinsamen Auswaertsfahrt der Saison gekauft",
     "Die Reise als eigentliches Fan-Erlebnis statt des Ergebnisses",
     ["Auswaertsfahrt Shirt", "Fussball Fan Geschenk", "Allesfahrer Shirt"]),
    ("Stehplatz",
     "Eine einzelne stilisierte Stadionstufe mit Wellenbrecher-Gelaender als schlichte Linienform, "
     "darauf ein kleiner Schal, frontal und freigestellt.",
     "Stehplatz", ["Betongrau", "Rot"],
     "Fans der Stehplatzkultur, die nie sitzen wollen",
     "Wird als Geschenk fuer den Dauerkarteninhaber zur neuen Saison gekauft",
     "Die Stehplatzkultur als Lebensgefuehl, vereinsuebergreifend",
     ["Stehplatz Shirt", "Dauerkarte Geschenk", "Stadion Fan Shirt"]),
    ("Ball im Netz",
     "Ein Fussball, der in ein aus wenigen Linien gezeichnetes Tornetz ausbeult, nur die Netzecke "
     "sichtbar, stark reduziert und freigestellt.",
     "Drin.", ["Weiss", "Schiefergrau"],
     "Stuermer und Fans, die fuer genau diesen Moment ins Stadion gehen",
     "Wird nach dem ersten Saisontor des Freundes als Geschenk gekauft",
     "Das kuerzeste Glueck im Fussball als ein einziges Wort",
     ["Tor Shirt", "Stuermer Geschenk", "Fussball Spruch kurz"]),
    ("Ecke",
     "Eine Eckfahne mit leicht wehendem Tuch und dem Viertelkreis am Boden, aus klaren Linien, "
     "ohne Farben eines Vereins, freigestellt.",
     "Kurz ausgeführt.", ["Neonorange", "Anthrazit"],
     "Taktikfans und Spieler, die Standards trainieren",
     "Wird als lustiges Geschenk fuer den Standardspezialisten im Team gekauft",
     "Ein unterschaetztes Spielelement mit Insider-Humor",
     ["Eckball Shirt", "Fussball Insider Geschenk", "Eckfahne Motiv"]),
    ("Anpfiff-Kaffee",
     "Eine dampfende Kaffeetasse, deren Dampf sich zu einem angedeuteten Fussball formt, schlichte "
     "Linienzeichnung, mittig freigestellt.",
     "Erst Kaffee, dann Anpfiff", ["Kaffeebraun", "Creme"],
     "Fussballfans, die morgens vor dem Amateurspiel nicht ohne Kaffee koennen",
     "Wird als Tasse fuer den Kaffeeliebhaber im Fussballverein verschenkt",
     "Verbindet zwei Alltagsrituale zu einem sympathischen Motiv",
     ["Fussball Tasse", "Kaffee Fussball Geschenk", "Anpfiff Spruch"]),
    ("Stollen",
     "Eine Schuhsohle mit sechs runden Stollen von unten gesehen, als Stempelabdruck mit leicht "
     "rauer Kante, freigestellt.",
     "Spuren hinterlassen.", ["Erdbraun", "Weiss"],
     "Spieler, die Rasen, Asche und Kunstrasen gleichermassen lieben",
     "Wird als Abschiedsgeschenk fuer einen langjaehrigen Mitspieler gekauft",
     "Der Abdruck als Symbol fuer Einsatz, ohne Markenschuh",
     ["Fussballschuh Motiv", "Fussball Abschied Geschenk", "Stollen Shirt"]),
    ("Pokal ohne Namen",
     "Ein schlichter zweihenkliger Pokal als reine Silhouette mit einem kleinen Stern im Inneren, "
     "ohne Gravur, frontal und freigestellt.",
     "Aufstieg!", ["Gold", "Schwarz"],
     "Amateurmannschaften, die gerade eine Meisterschaft gefeiert haben",
     "Wird von der Mannschaft als gemeinsames Aufstiegsshirt bestellt",
     "Eigener Triumph der eigenen Truppe statt fremder Titel",
     ["Aufstieg Shirt", "Meister Shirt Mannschaft", "Pokal Motiv"]),
    ("Halbzeit",
     "Eine Sanduhr, deren obere und untere Haelfte je eine Spielfeldhaelfte zeigen, klare Linien, "
     "reduziert und freigestellt.",
     "Halbzeit", ["Sandbeige", "Rasengruen"],
     "Fans, die in der Halbzeitpause gern philosophieren",
     "Wird als Geschenk zum dreissigsten oder vierzigsten Geburtstag gekauft",
     "Doppeldeutig: Spielhalbzeit und Lebenshalbzeit mit Humor",
     ["Halbzeit Geburtstag Shirt", "40 Geburtstag Fussball", "Fussball Geschenk Humor"]),
    ("Fanschal-Knoten",
     "Ein einfach geknoteter Strickschal mit Fransen, nur grobe Streifen ohne Schrift, locker "
     "gebunden und freigestellt, sehr reduziert.",
     "", ["Winterrot", "Wollweiss"],
     "Fans, die im Winter jedes Spiel mit Schal besuchen",
     "Wird vor der Winterpause als warmer Hoodie verschenkt",
     "Der Schal als universelles Fan-Symbol ohne Vereinsbezug",
     ["Fanschal Motiv", "Fussball Hoodie Winter", "Fan Geschenk Winter"]),
    # ---------------------------------------------------------- Deutschland
    ("Aschenplatz",
     "Ein kleines Viereck aus roter Asche mit einer einzelnen weissen Kalklinie und einem alten "
     "Ball darauf, von oben gesehen, reduziert.",
     "Asche an den Knien", ["Ziegelrot", "Kalkweiss"],
     "Spieler, die auf Aschenplaetzen gross geworden sind",
     "Wird als nostalgisches Geschenk unter alten Mannschaftskollegen gekauft",
     "Nostalgie der Aschenplaetze trifft eine ganze Generation",
     ["Aschenplatz Shirt", "Fussball Nostalgie Geschenk", "Retro Fussball Deutschland"]),
    ("Bratwurst und Ball",
     "Eine Bratwurst im Broetchen und ein Fussball nebeneinander als zwei schlichte Formen, "
     "gleiche Linienstaerke, freigestellt.",
     "Grundnahrungsmittel", ["Senfgelb", "Braun"],
     "Stadiongaenger, fuer die die Wurst zum Spieltag gehoert",
     "Wird als lustige Tasse fuer den Grillmeister im Fanclub gekauft",
     "Der Stadionimbiss als kultureller Teil des Spieltags",
     ["Stadionwurst Shirt", "Fussball Humor Geschenk", "Grill Fussball Tasse"]),
    # ---------------------------------------------------------- England
    ("Box to Box",
     "Zwei schmale Strafraum-Rechtecke links und rechts, verbunden durch eine einzige gestrichelte "
     "Laufweglinie, sehr minimalistisch und freigestellt.",
     "Box to Box", ["Weiss", "Marineblau"],
     "Laufstarke Mittelfeldspieler und englische Fussballfans",
     "Wird als Geschenk fuer den unermuedlichen Laeufer im Team gekauft",
     "Englischer Fachbegriff als Stilstatement ohne Clubbezug",
     ["Box to Box Shirt", "Mittelfeld Geschenk", "Englischer Fussball Shirt"]),
    ("Regen und Rasen",
     "Eine kleine Regenwolke mit schraegen Regenstrichen ueber einem Rasenstueck mit Ball, klare "
     "Linien, reduziert und freigestellt.",
     "Proper weather for football", ["Regengrau", "Rasengruen"],
     "Fans englischer Fussballkultur, die jedes Wetter aushalten",
     "Wird als Hoodie fuer kalte Abendspiele im Herbst gekauft",
     "Britischer Humor ueber Schmuddelwetter und echten Fussball",
     ["Englischer Fussball Hoodie", "Regen Fussball Shirt", "Fussball Humor Englisch"]),
    # ---------------------------------------------------------- Spanien
    ("Tiki-Taka",
     "Kurze Passlinien als Dreiecke zwischen fuenf kleinen Punkten, die ein Rautenmuster bilden, "
     "geometrisch und sehr reduziert.",
     "Tiki-Taka", ["Rot", "Senfgelb"],
     "Fans schnellen Kurzpassspiels und spanischer Fussballkultur",
     "Wird als Geschenk fuer den Technikliebhaber in der Freizeitliga gekauft",
     "Der allgemeine Spielstil als Grafik statt eines bestimmten Teams",
     ["Tiki Taka Shirt", "Kurzpass Fussball Motiv", "Spanischer Fussball Geschenk"]),
    ("Siesta nach dem Sieg",
     "Ein Fussball liegt im Schatten eines schlichten Sonnenschirms, zwei klare Formen mit einer "
     "Sonne als Kreis, reduziert und freigestellt.",
     "Primero fútbol, luego siesta", ["Terrakotta", "Sonnengelb"],
     "Fussballfans mit Liebe zu Spanien und entspannten Sommerabenden",
     "Wird als Urlaubsshirt fuer die Spanienreise im Sommer gekauft",
     "Spanisches Lebensgefuehl kombiniert mit dem Spieltag",
     ["Spanien Fussball Shirt", "Siesta Spruch", "Urlaub Fussball Geschenk"]),
    # ---------------------------------------------------------- Italien
    ("Catenaccio",
     "Vier schlichte senkrechte Balken in einer Reihe wie eine Abwehrkette, davor ein Ball, "
     "geometrisch und sehr reduziert.",
     "Catenaccio", ["Dunkelblau", "Weiss"],
     "Verteidiger und Fans kompakter italienischer Defensivkunst",
     "Wird als augenzwinkerndes Geschenk fuer den Abwehrstrategen gekauft",
     "Taktikgeschichte als stilvolles Wort statt Vereinslogo",
     ["Catenaccio Shirt", "Italien Fussball Geschenk", "Abwehr Taktik Motiv"]),
    ("Calcio e Caffè",
     "Eine kleine Espressotasse auf Untertasse, daneben ein Fussball in gleicher Groesse, "
     "schlichte Linien, ruhig und freigestellt.",
     "Calcio e caffè", ["Espressobraun", "Creme"],
     "Liebhaber italienischer Kaffee- und Fussballkultur",
     "Wird als Tasse fuer den Espresso-Fan und Fussballliebhaber verschenkt",
     "Italienische Kaffee- und Genusskultur statt einer bestimmten Clubtradition",
     ["Calcio Tasse", "Espresso Fussball Geschenk", "Italien Fan Tasse"]),
    ("Tifoso",
     "Eine erhobene Hand mit einem einfachen Schal zwischen den Fingern, als klare Silhouette mit "
     "wenigen Linien, ohne Schrift, freigestellt.",
     "Tifoso per sempre", ["Tiefgruen", "Weiss"],
     "Leidenschaftliche Fans mit Faible fuer italienische Stadionkultur",
     "Wird vor einer Italienreise mit Stadionbesuch gekauft",
     "Die Leidenschaft der Tifosi als allgemeines Gefuehl",
     ["Tifoso Shirt", "Italien Fussball Fan", "Fan Leidenschaft Motiv"]),
    # ---------------------------------------------------------- Frankreich
    ("Petit Pont",
     "Zwei stilisierte Beine als schmale Bogenform, durch die ein Ball rollt, nur Linien, verspielt "
     "aber sehr reduziert und freigestellt.",
     "Petit pont", ["Blau", "Weiss"],
     "Technisch verspielte Spieler und Fans franzoesischer Fussballkultur",
     "Wird als Geschenk fuer den Tunnelkoenig im Freizeitkick gekauft",
     "Der Tunnel als Kunstform mit franzoesischem Charme",
     ["Tunnel Fussball Shirt", "Petit Pont Motiv", "Fussball Trick Geschenk"]),
    ("Croissant und Ball",
     "Ein Croissant und ein Fussball auf einer schmalen Rasenlinie nebeneinander, zwei klare "
     "Formen in gleicher Groesse, freigestellt.",
     "Football et croissants", ["Buttergelb", "Marineblau"],
     "Frankreich-Liebhaber, die Fussball und Fruehstueck feiern",
     "Wird als Urlaubsgeschenk nach einer Reise nach Frankreich gekauft",
     "Leichter franzoesischer Humor ohne Club- oder Verbandsbezug",
     ["Frankreich Fussball Shirt", "Croissant Motiv", "Fussball Humor Geschenk"]),
    # ---------------------------------------------------------- Tuerkei
    ("Çay und Maç",
     "Ein tulpenfoermiges Teeglas auf kleiner Untertasse neben einem Fussball, schlichte klare "
     "Linien, ruhig und freigestellt.",
     "Çay & Maç", ["Teerot", "Gold"],
     "Fussballfans mit tuerkischen Wurzeln und Teeliebhaber",
     "Wird als Tasse fuer den Vater verschenkt, der jedes Spiel mit Tee schaut",
     "Tee und Spiel als Familienritual, herzlich und vereinsfrei",
     ["Cay Mac Tasse", "Tuerkischer Fussball Geschenk", "Teeglas Fussball Motiv"]),
    ("Derbi Günü",
     "Zwei gegenueberliegende einfache Fanschal-Silhouetten, die sich zu einem Kreis schliessen, "
     "ohne Farben oder Schrift eines Vereins, reduziert.",
     "Derbi Günü", ["Schwarz", "Weiss"],
     "Fans, fuer die das Derby der wichtigste Tag des Jahres ist",
     "Wird vor dem grossen Stadtderby gemeinsam mit Freunden gekauft",
     "Derbygefuehl als Kulturphaenomen in tuerkischer Sprache",
     ["Derby Shirt", "Derbi Gunu Motiv", "Tuerkei Fussball Shirt"]),
    # ---------------------------------------------------------- Rituale & Emotion
    ("Daumen drücken",
     "Zwei gekreuzte Finger als klare Silhouette, darin ein kleiner Fussball als Punkt, schlicht "
     "und freigestellt.",
     "Heute klappt's.", ["Senfgelb", "Schwarz"],
     "Aberglaeubische Fans mit festen Spieltagsritualen",
     "Wird vor einem Entscheidungsspiel als Gluecksbringer gekauft",
     "Aberglaube am Spieltag als sympathischer Insider",
     ["Gluecksbringer Fussball", "Fussball Aberglaube Shirt", "Daumen druecken Motiv"]),
    ("Herzschlag",
     "Eine EKG-Linie, deren Ausschlag in der Mitte einen Fussball formt, eine einzige durchgehende "
     "Linie, sehr reduziert.",
     "", ["Signalrot", "Weiss"],
     "Fans, deren Puls bei jedem Spiel steigt",
     "Wird als Geschenk zum Valentinstag fuer den fussballverrueckten Partner gekauft",
     "Das bekannte Herzschlag-Motiv neu fuer Fussball gedacht",
     ["Fussball Herzschlag Shirt", "Fussball Liebe Geschenk", "EKG Fussball Motiv"]),
    ("Kleine Kicker",
     "Ein kleiner Fussballschuh neben einem grossen Fussballschuh, beide als schlichte Umrisse, "
     "nebeneinander stehend und freigestellt.",
     "Gleicher Verein. Anderes Alter.", ["Rasengruen", "Weiss"],
     "Vaeter, Muetter und Kinder, die gemeinsam kicken",
     "Wird als Partnerlook fuer Eltern und Kind zum Vatertag gekauft",
     "Familie und Fussball als generationsuebergreifendes Band",
     ["Vater Sohn Fussball Shirt", "Partnerlook Fussball", "Vatertag Fussball Geschenk"]),
    ("Rasenmäher-Streifen",
     "Ein rechteckiges Rasenstueck mit abwechselnd hellen und dunklen Maehstreifen, ganz leicht "
     "perspektivisch, sonst nichts.",
     "Frisch gemäht", ["Hellgruen", "Dunkelgruen"],
     "Platzwarte, Gaertner und Fans gepflegter Stadionrasen",
     "Wird als Dankeschoen fuer den ehrenamtlichen Platzwart gekauft",
     "Wuerdigung der Ehrenamtlichen hinter jedem Spieltag",
     ["Platzwart Geschenk", "Rasen Motiv Shirt", "Ehrenamt Verein Geschenk"]),
    ("Einwurf",
     "Zwei Haende halten einen Ball ueber dem Kopf, als reine Linienzeichnung ohne Koerper, "
     "symmetrisch und freigestellt.",
     "Beide Füße am Boden!", ["Weiss", "Anthrazit"],
     "Jugendtrainer und Amateure, die den Einwurf-Fehler zu gut kennen",
     "Wird als lustiges Geschenk fuer den Jugendtrainer gekauft",
     "Die haeufigste Trainingsregel als humorvoller Spruch",
     ["Jugendtrainer Geschenk", "Einwurf Spruch", "Fussball Trainer Humor"]),
    ("Abseits erklärt",
     "Drei kleine Punkte und eine gestrichelte Linie, die eine Abseitsstellung zeigt, wie auf "
     "einer Erklaerskizze, sehr reduziert und freigestellt.",
     "Ich erklär's dir nochmal.", ["Kreideweiss", "Schiefergrau"],
     "Fans, die Freunden immer wieder die Abseitsregel erklaeren",
     "Wird als augenzwinkerndes Geschenk fuer den Regelexperten gekauft",
     "Die beruehmteste Regel als Insider-Humor",
     ["Abseits Shirt", "Fussball Regel Humor", "Fussball Geschenk lustig"]),
    ("Letzte Saison",
     "Ein Paar Fussballschuhe haengt an zusammengebundenen Schnuersenkeln an einem Nagel, klare "
     "Linien, ruhig und freigestellt.",
     "Die Schuhe hängen. Die Liebe bleibt.", ["Warmgrau", "Rasengruen"],
     "Ehemalige Spieler, die ihre Karriere beendet haben",
     "Wird zum Abschiedsspiel eines langjaehrigen Mitspielers verschenkt",
     "Emotionaler Karriereabschluss fuer Amateure, nicht fuer Stars",
     ["Fussball Abschied Geschenk", "Karriereende Shirt", "Fussballschuhe Motiv"]),
]


def eintraege() -> list[trends.Trend]:
    """Die Kollektion als Empfehlungen im Format der Websuche."""
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
