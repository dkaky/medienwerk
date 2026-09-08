"""Motiv-Radar: lesen, was in fremden Shops laeuft.

Vier Stufen, jede in einer eigenen Datei:

1. ``quellen``    - Shop-Link rein, Plattform + Shop + Listen-URL raus.
2. ``ernte``      - Browser holt die Artikel samt Verkaufssignal.
3. ``signale``    - aus Titel und Zahlen wird Thema, Stichwort und Rangwert.
4. ``umwandlung`` - aus dem Thema wird ein Prompt fuer ein EIGENES Motiv.

Die Grenze, die durch alle vier Stufen laeuft: uebernommen wird das THEMA,
niemals der Wortlaut und niemals die Gestaltung. Deshalb steht der fremde Titel
in der Datenbank ausschliesslich als Beleg der Herkunft - Stufe 4 weigert sich,
ihn in einen Prompt zu schreiben.
"""
