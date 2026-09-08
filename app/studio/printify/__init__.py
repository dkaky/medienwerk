"""Printify-Anbindung des Studios.

Der Handelsteil kennt nur AliExpress als Bezugsquelle. Print-on-Demand ist die
zweite: kein Lager, keine Vorkasse, gedruckt wird erst nach dem Verkauf.
"""

from app.studio.printify.client import PrintifyClient, PrintifyFehler
from app.studio.printify.factory import build_product_payload, fit_scale

__all__ = ["PrintifyClient", "PrintifyFehler", "build_product_payload", "fit_scale"]
