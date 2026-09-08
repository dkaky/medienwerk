"""Schutzfilter des Studios.

Der Handelsteil hat eine Markenliste im KI-Prompt - die MELDET einen Treffer,
blockt ihn aber nicht. Fuer eigene Motive genuegt das nicht: hier entscheidet
ein harter Filter VOR der Erzeugung, ob ein Motiv ueberhaupt entstehen darf.
"""

from app.studio.safety.errors import FilterFehler
from app.studio.safety.ip_filter import FilterResult, IPFilter

__all__ = ["FilterFehler", "FilterResult", "IPFilter"]
