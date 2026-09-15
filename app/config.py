"""Zentrale Konfiguration – aus .env via pydantic-settings.

Alle Module beziehen Einstellungen ueber `get_settings()`. Secrets liegen
ausschliesslich in der .env (nie im Code), siehe Spec Kap. 6.2.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Allgemein ---
    app_env: str = "development"
    log_level: str = "INFO"
    use_mocks: bool = True
    # Lokal abschaltbar: Wiederherstellung darf keine Handelsjobs ausloesen.
    background_jobs_enabled: bool = True
    # Growth Engine V1 kill switch. Default OFF: deployment alone must neither
    # expose its UI/API nor register or execute its recurring jobs.
    growth_engine_enabled: bool = False
    # Studio-Trakt (eigene Motive, Print-on-Demand). Aus = System laeuft wie bisher.
    studio_enabled: bool = False
    # Tagesbudget in USD: -1 = ausdruecklich unbegrenzt, 0 = gesperrt.
    studio_daily_budget_usd: float = 0.0
    openai_api_key: str = ""
    # fal.ai (Flux) - guenstiger als gpt-image-1, aber NIE Text rendern lassen
    fal_api_key: str = ""
    studio_image_dir: str = "./data/studio_images"
    # Print-on-Demand: kein Lager, keine Vorkasse - gedruckt wird nach dem Verkauf
    printify_token: str = ""
    printify_shop_id: str = ""
    # Spreadshirt (Public Shop API). Zweiter Verkaufskanal, keine zweite Druckerei:
    # diese Schnittstelle liest Shop und Artikel und baut Warenkoerbe - Motive
    # hochladen kann sie nicht. Leer = Kanal aus.
    # EU-Plattform (api.spreadshirt.net) und NA-Plattform (.com) haben GETRENNTE
    # Keys; ein EU-Key an der NA-Basis antwortet mit 401.
    spreadshirt_api_key: str = ""
    spreadshirt_api_secret: str = ""   # nur fuer signierte Aufrufe (/api/v1)
    spreadshirt_shop_id: str = ""
    spreadshirt_base_url: str = "https://api.spreadshirt.net/api/v1"
    # Pflichtangabe, kein Schmuck: Spreadshirt sperrt Anfragen ohne aussagekraeftigen
    # User-Agent. Format: "Name/Version (URL; Mail)".
    spreadshirt_user_agent: str = "Medienwerk-POD-Shop/1.0"
    # Dashboard-Login: leer = Auth AUS (lokale Entwicklung/Tests). Fuer Betrieb auf
    # einem Server/VPS ZWINGEND setzen – sonst ist der Shop offen im Netz.
    dashboard_password: str = ""
    # Per-Integration-Override (None = folgt use_mocks). So kann man z. B. LLM +
    # eBay echt nutzen, AliExpress aber gemockt lassen (Extraktion noch offen).
    mock_llm: bool | None = None
    mock_ebay: bool | None = None
    mock_aliexpress: bool | None = None
    mock_autods: bool | None = None

    # --- Datenbank ---
    database_url: str = "sqlite:///./data/ebay_store.db"

    # --- eBay ---
    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    ebay_refresh_token: str = ""
    # RuName = eBays Name fuer die hinterlegte Weiterleitungs-Adresse der eigenen App
    # (developer.ebay.com -> User Tokens). KEIN Geheimnis, aber ohne sie laesst sich
    # der Refresh-Token nicht holen. In der .env, damit scripts.ebay_oauth sie nicht
    # bei jedem Aufruf als Argument braucht.
    ebay_runame: str = ""

    @field_validator(
        "ebay_client_id", "ebay_client_secret", "ebay_refresh_token",
        mode="before",
    )
    @classmethod
    def _blank_commented_out_credential(cls, v: object) -> object:
        """Ein aus der .env geladener Inline-Kommentar (z. B. ``KEY=   # entfernt …``)
        darf NIE als echte Zugangsdaten durchgehen. Sonst meldet die App faelschlich
        ``ebay_live`` (system.py) und laeuft bei echten eBay-Calls in ein
        401 ``invalid_client``. Ein Wert, der mit ``#`` beginnt, gilt als leer/unset.
        """
        if isinstance(v, str) and v.strip().startswith("#"):
            return ""
        return v
    ebay_ipn_verification_token: str = "changeme"   # 32-80 Zeichen (Marketplace Account Deletion)
    ebay_deletion_endpoint_url: str = ""   # exakte oeffentliche HTTPS-URL des Deletion-Endpunkts
    # Loeschmeldungen nimmt eine Supabase-Funktion im Lovable-Projekt an (der PC ist
    # nicht rund um die Uhr erreichbar); das Programm holt sie hier ab.
    # Siehe deploy/supabase-ebay-loeschung/ und app/services/ebay_loeschmeldungen.py.
    ebay_loesch_abhol_url: str = ""
    ebay_loesch_abhol_token: str = ""

    # --- Motiv -> eBay-Angebot (app/studio/ebay_weg.py) ---
    # Automatisch nach jeder Erzeugung einstellen. Greift NUR mit MOCK_EBAY=false -
    # im Probebetrieb bleibt die Schreibsperre fuer Automatik zu, der Knopf im
    # Studio kommt trotzdem durch.
    ebay_auto_veroeffentlichen: bool = False
    # Groessen der Textilien. eBay kennt "2XL", NICHT "XXL" (Taxonomie EBAY_DE,
    # abgefragt 13.09.2026) - ein freier Wert faellt aus dem Groessenfilter.
    # Gilt fuer T-Shirt, Polo und Hoodie; Oversize hat seinen eigenen Lauf (S-3XL).
    ebay_textil_groessen: str = "XS,S,M,L,XL,2XL,3XL"
    ebay_menge_je_variante: int = 10                 # auf Bestellung gedruckt
    ebay_produktfarbe: str = "Weiß"                  # Farbe der Ware im Produktbild
    ebay_marke: str = "medienwerk"
    # Preise inkl. Versand ueberschreiben, z. B. "tshirt=15.90,tasse=12.90".
    # Dezimalpunkt, kein Komma - das Komma trennt die Produkte.
    # Leer -> Preise aus dem Katalog in app/studio/ebay_weg.py.
    ebay_preise: str = ""

    # --- Echte Produktfotos (app/integrations/dynamic_mockups.py) ---
    # Ohne Schluessel oder ohne Vorlagen bleiben die gezeichneten Notbilder.
    dynamic_mockups_api_key: str = ""
    mockup_vorlagen_datei: str = "./data/mockup_vorlagen.json"
    # Eigene Chroma-Key-Vorlagen fuer die lokale Montage (app/studio/mockup_montage.py).
    # Liegen dort alle Ansichten eines Produkts, braucht es keinen Dynamic-Mockups-Schluessel.
    mockup_montage_ordner: str = "./data/mockup_vorlagen"

    # --- Trend-Radar (app/studio/radar/trends.py) ---
    # Websuche ueber die OpenAI Responses API. Laut Doku (13.09.2026) koennen das
    # u. a. gpt-4.1-mini, gpt-4.1 und gpt-5.5 - Vorgabe ist das guenstigste.
    trend_modell: str = "gpt-4.1-mini"
    # Motivname + Beschreibung fuer eBay (app/studio/verkaufstext.py) - sieht das Motivbild.
    verkaufstext_modell: str = "gpt-4.1-mini"
    trend_anzahl: int = 12
    # Taeglich morgens von selbst suchen (braucht BACKGROUND_JOBS_ENABLED=true).
    trend_radar_taeglich: bool = False
    ebay_marketplace_id: str = "EBAY_DE"   # EBAY_DE | EBAY_US | EBAY_GB | ...
    ebay_use_sandbox: bool = False         # True -> api.sandbox.ebay.com
    # Merchant-Location (Pflicht fuer publishOffer; WAREHOUSE, kein Ladengeschaeft noetig)
    # Bestimmt die "Versand aus"-Angabe der eBay-Listings -> muss der echte Standort sein.
    ebay_merchant_location_key: str = "MW-DE-01"
    ebay_warehouse_address_line: str = ""   # -> .env: EBAY_WAREHOUSE_ADDRESS_LINE
    ebay_warehouse_postal: str = ""        # -> .env: EBAY_WAREHOUSE_POSTAL
    ebay_warehouse_city: str = ""          # -> .env: EBAY_WAREHOUSE_CITY
    ebay_warehouse_state: str = ""         # -> .env: EBAY_WAREHOUSE_STATE
    ebay_warehouse_country: str = "DE"
    # Optionale feste Policy-IDs (leer = automatisch; Versand bevorzugt kostenlos)
    ebay_payment_policy_id: str = ""
    ebay_fulfillment_policy_id: str = ""
    ebay_return_policy_id: str = ""
    # Schnelle Versandbedingung fuer LOKALE Quellen/Eigenbestand (Fokus 08/2026):
    # per NAME aufgeloest (Account API), damit keine ID in die .env muss. Neue Offers
    # mit lokaler Quelle (delivery_days <= fast_shipping_max_days) oder Eigenbestand
    # bekommen sie automatisch; leer = Funktion aus. Bestehende Listings werden NIE
    # automatisch umgestellt (Vorfall: fruehere Pauschal-Aktion).
    ebay_fulfillment_policy_fast_name: str = ""   # im eigenen Konto (noch) nicht angelegt
    fast_shipping_max_days: int = 7        # bis hierhin gilt eine Quelle als "lokal"
    fast_shipping_auto_policy: bool = True # Kill-Switch fuer die Auto-Zuweisung
    # Nutzer-Entscheid 08.08.: beim Livegang neuer Listings automatisch den empfohlenen
    # Mengenrabatt aktivieren, wenn die Marge eine Staffel traegt (bewusste Ausnahme
    # von der Nur-per-Klick-Linie; False = zurueck zu propose-only).
    multibuy_auto_activate_on_publish: bool = True
    # Kontist-Geschaeftskonto (Nur-Lese, Nutzer-Projekt 10.08.): OAuth2-Client aus
    # https://kontist.dev/client-management. Leer = Feature aus. Secrets NUR in der
    # Server-.env (Uebertragung per Workflow kontist-env.yml aus GitHub-Secrets).
    kontist_client_id: str = ""
    kontist_client_secret: str = ""
    # Wohin Kontist nach dem Zustimmen zurueckschickt. MUSS gesetzt sein, bevor der
    # OAuth-Weg benutzt wird. Bei diesem Rueckruf haengt der Autorisierungscode in
    # der Adresszeile - eine fest eingetragene fremde Adresse wuerde ihn dorthin
    # tragen. Deshalb gibt es keine Vorgabe: leer heisst, die Kontist-Anmeldung
    # bricht mit klarer Meldung ab.
    kontist_redirect_uri: str = ""
    # Lokal-Fokus (10.08.): Kampagnen-/Hub-Seite als Kandidaten-Quelle der taeglichen
    # Trend-Suche. AliExpress blockiert Suchseiten fuer den Headless-Harvester und die
    # DS-API kennt keinen "Versand aus"-Filter — diese Nutzer-URL (Local+/Versand aus DE)
    # ist die verifizierte Quelle (Ernte-Test 10.08.: 20 IDs).
    local_source_page_url: str = "https://www.aliexpress.com/ssr/300002243/Zz2BHFNHKA"

    # --- AutoDS / Fulfillment-Engine ---
    autods_api_key: str = ""
    autods_base_url: str = "https://api.autods.com/v2"
    # "native" = AutoDS komplett ersetzen (Listing direkt via eBay Sell Inventory API);
    # "autods" = ueber AutoDS (Mock/Real). Default native -> kein AutoDS-Abo noetig.
    fulfillment_engine: str = "native"
    # Sicherheit: AliExpress-Bestellung gibt ECHTES Geld aus. Default = manuelle
    # Freigabe pro Bestellung. Auf True nur setzen, wenn voll automatisch gewuenscht.
    auto_fulfill: bool = False
    # Karenzzeit (Min) vor Auto-Bestellung: Puffer fuer Kaeufer-Storno/Adressaenderung.
    # 0 = sofort beim naechsten Order-Poll bestellen (Nutzerwunsch).
    auto_fulfill_grace_minutes: int = 0
    # Auto-Fulfillment MARGE-SPERRE: automatisch NUR bestellen, wenn die aktuelle
    # Marge (VK - Gebuehren - EK)/VK >= dieser Schwelle liegt. Sonst -> manuelle
    # Freigabe (kein Blindkauf mit Verlust). Nutzt target_margin_pct als Vorgabe.
    auto_fulfill_min_margin_pct: float = 0.20
    # Carrier-Code fuer die Tracking-Rueckmeldung an eBay (createShippingFulfillment).
    # "Other" = generisch (Kaeufer sieht die Sendungsnummer); AliExpress/Cainiao hat
    # keinen eigenen eBay-Standardcode, daher "Other" als sichere Vorgabe.
    ebay_shipping_carrier_code: str = "Other"
    # "Unterwegs" -> "Zugestellt" automatisch nach so vielen Tagen (seit Verkaufsdatum).
    # eBay/AliExpress melden kein echtes Zustell-Ereignis; ein Paket nach DE ist nach
    # dieser Frist praktisch sicher angekommen. Storno/Erstattung bleiben unberuehrt.
    # 0 = Auto-Hochstufung aus. (Nutzerwunsch: 40 Tage.)
    tracking_delivered_after_days: int = 40
    # Echte Zustell-Erkennung: „Unterwegs" -> „Zugestellt" sobald der Zusteller-Status
    # (AliExpress/Cainiao get_tracking) eine Zustellung meldet – laeuft im Tracking-/Sync-Lauf,
    # unabhaengig von der 40-Tage-Heuristik. False = aus (nur Zeit-Heuristik).
    auto_delivered_from_tracking: bool = True
    # DHL Shipment-Tracking-API (Abfrage): ECHTES Zustelldatum per Sendungsnummer, wo AliExpress
    # die DHL-Zustellung nicht meldet. Key NUR aus der Umgebung (DHL_API_KEY), NIE im Code.
    # Leer = aus (dann nur AliExpress-Status + Zeit-Heuristik). Free-Tier: 250/Tag, 1/5s.
    dhl_api_key: str = ""
    dhl_daily_cap: int = 230        # Sicherheits-Tagesbudget unter dem 250er-Free-Limit
    # Titel-Optimierung: eBay zeigt bis 80 Zeichen im Such-Snippet. Ziel: die Kapazitaet
    # keyword-reich ausschoepfen (echte Merkmale, KEINE Werbe-Floskeln). Unter der
    # Warn-Schwelle wird im UI ein Hinweis "Keyword-Potenzial ungenutzt" gezeigt.
    title_target_min_chars: int = 78   # darunter laesst der Code den Titel auffuellen (Ziel ~80)
    title_warn_below_chars: int = 60
    # Preissenkungen (KI-Marktanalyse): die Optimierung darf Preise Richtung Marktniveau
    # SENKEN, aber nie unter diese Netto-Marge (nach eBay-Gebuehren). Nutzerregel: nicht
    # stur 8 € Mindestgewinn erzwingen – solange >= 20 % Marge bleibt, ist es ok.
    allow_price_lowering: bool = True
    lowering_min_margin_pct: float = 0.20
    # Aufraeum-/Lösch-Empfehlung: Listing kommt in den cleanup-Bucket, wenn es nie
    # verkauft wurde, seit mind. so vielen Tagen online ist und 0 Traffic hat. Beim
    # Öffnen zusätzlich Konkurrenz-Check: >X% über Median UND Preis kann nicht unter den
    # 20%-Boden runter = "löschen erwägen". NUR Empfehlung, nie automatisch löschen.
    cleanup_min_age_days: int = 60
    cleanup_price_premium_pct: float = 0.15
    # Listings mit bis zu so vielen Impressionen (aber 0 Klicks, nie verkauft) zaehlen als
    # "wird gesehen, aber niemand klickt" und kommen ins Aufraeumen.
    # KORREKTUR 02.08.2026: Der alte Wert 5 war unerreichbar - gemessen an 725 aktiven
    # Listings hatte KEIN einziges <= 5 Impressionen (Minimum 11, Median 150). Der Reiter
    # "Aufraeumen" war dadurch dauerhaft leer. 100 trifft die echten Ladenhueter
    # (Nutzerregel: "Produkte die sich verkaufen lassen, verkaufen sich schnell").
    cleanup_max_impressions: int = 100
    # Anzeigenrate-Empfehlung (Promoted Listings): statt fix Basis+3% wird die HÖCHSTE
    # Rate empfohlen, bei der die Zielmarge noch hält – gedeckelt auf diesen Wert.
    ad_rate_hard_cap_pct: float = 0.20
    # Multi-Buy-Rabatt (Mengenrabatt, propose-only): nur empfehlen, wenn die Ist-Marge
    # mindestens so hoch ist; Staffel-Deckel; und JEDE Staffel muss je Zusatz-Einheit noch
    # mindestens diesen Deckungsbeitrag bringen UND >= lowering_min_margin_pct Marge halten.
    multibuy_min_margin_pct: float = 0.25
    multibuy_max_discount_pct: float = 0.10
    multibuy_min_extra_profit_eur: float = 3.0
    # AUTOMATIK: aktive Mengenrabatte taeglich entfernen, wenn KEINE Staffel mehr echten
    # Mehrgewinn bringt (Basis-Marge egal). Nur ENTFERNEN, nie hinzufuegen. Abschaltbar per
    # MULTIBUY_AUTO_REMOVE=false. (Nutzerwunsch 16.07.; Geld-sicher: unbestaetigte Quelle/fehlende
    # Daten werden nie angefasst.)
    multibuy_auto_remove: bool = True

    # --- Pricing / Repricing (Modell nach eigenem Kalkulator, § 19 = keine USt) ---
    # Gewinn = max(Kosten*profit_pct, profit_eur, min_profit_eur);
    # Preis = (Kosten + Gewinn + Fixkosten) / (1 - fee_pct), MINDESTENS aber der Preis,
    # der target_margin_pct traegt; dann auf Cent-Endung runden.
    #
    # Die Marge-Untergrenze kam am 29.08.2026 dazu. Vorher rechnete dieses Modell nur
    # einen AUFSCHLAG AUF DIE KOSTEN (Kosten*0,20), das Upload-Modell dagegen eine
    # MARGE VOM VERKAUFSPREIS (20 % von VK). Beide hiessen "20 %" und meinten
    # Verschiedenes: bei 9,08 EUR Ware schlug das alte Modell 23,95 EUR vor (33 %
    # Marge), das Upload-Modell 18,95 EUR (23 %). Im Dashboard stand deshalb dauerhaft
    # ein zu hoher Vorschlag neben einem korrekten Preis.
    # 1.0 ist NUR richtig, wenn AliExpress schon in Euro liefert - also wenn
    # aliexpress_target_currency auf EUR steht. Die Kombination CNY + 1.0 wuerde
    # Yuan als Euro verbuchen (Faktor ~7,8 zu hoch); _waehrung_passt() unten
    # laesst sie deshalb nicht mehr durch.
    cny_to_eur_rate: float = 1.0
    # AliExpress-DS-Bestellungen werden IMMER in USD belastet (trade.ds.order.get:
    # user_order_amount). Kurs fuer die EK-Erfassung in EUR; empirisch aus echten
    # Zahlungen kalibriert (13,37$=11,72€ bzw. 14,31$=12,54€ -> 0.8765).
    usd_to_eur_rate: float = 0.8765
    # eBay-Verkaufsprovision (OHNE Werbung!) als Anteil vom Verkaufspreis. Die Promoted-
    # Listings-Anzeigenrate kommt SEPARAT dazu (ebay_ad_rate_pct) — Gesamtgebuehr real
    # gemessen ~31,4 % + 0,45 € (13.07.: #1142/#1145) = 0.22 Provision + 0.10 Anzeigen.
    ebay_fee_pct: float = 0.22
    ebay_ad_rate_pct: float = 0.10       # Standard Promoted-Listings-Anzeigenrate je Listing (10%)
    ebay_fixed_fee_eur: float = 0.45     # Fixkosten je Verkauf (Fixed Fee €, NETTO/ohne MwSt)
    # eBay besteuert die GESAMTE Gebuehr (Provision + Fixbetrag + Anzeigen) mit MwSt
    # (DE 19%). Ohne diesen Faktor unterschaetzt die Kalkulation die Gebuehr um ~19 %
    # (Nachweis 14.07.: 19,95 € × 16 % + 0,45 € = 3,64 € -> inkl. MwSt 4,34 €).
    ebay_fee_vat_pct: float = 0.19
    shipping_cost_eur: float = 0.0       # eigene Versandkosten (Dropshipping i.d.R. 0)
    # AliExpress berechnet auf günstige Artikel Versand -> in die Kostenbasis einrechnen.
    aliexpress_free_shipping_threshold: float = 10.0  # EK < 10€ -> Versand fällt an
    aliexpress_shipping_fee_eur: float = 1.99         # Versandpauschale unter dem Schwellwert
    # KORREKTUR 14.07.2026: AliExpress schlaegt auf DS-Orders KEINEN prozentualen Aufschlag
    # auf, sondern eine PAUSCHALE geschaetzte Einfuhrgebuehr/Zoll von ~3,57 EUR (~4 USD) je
    # Bestellung - und das in ~90 % der Faelle. Der fruehere prozentuale Ansatz (32 %) war ein
    # Fehlschluss aus dem Sweep und liess EK-Schaetzungen bei teuren Artikeln explodieren
    # (32 % auf 50 EUR = +16 EUR statt der realen +3,57 EUR). Eine Pauschale ist preis-
    # unabhaengig korrekt. Der ECHTE EK je verkaufter Bestellung kommt weiter direkt von der
    # API (trade.ds.order.get) und faengt die restlichen ~10 % Sonderfaelle ab.
    customs_fee_eur: float = 3.57        # pauschale geschaetzte Einfuhrgebuehr je Order (~4 USD)
    # Prozentualer Aufschlag DEAKTIVIERT (0.0) - durch die Pauschale customs_fee_eur ersetzt.
    # Feld bleibt fuer den Fall, dass AliExpress je wieder prozentual abrechnet (env-tunbar).
    aliexpress_tax_pct: float = 0.0
    # HARTE EK-Obergrenze bei der Produkt-Suche (Nutzerregel 07.08.2026): keine Artikel ueber
    # diesem Einkaufspreis mehr einstellen. Grund: ab ~150 EUR Warenwert faellt bei der Einfuhr
    # eine ZOLLANMELDUNG + Zusatzkosten an (150-EUR-EU-Zollfreigrenze) -> teuer & kompliziert.
    # 140 EUR = bewusster Sicherheitspuffer darunter. Greift IMMER, zusaetzlich zur optionalen
    # EK-Spanne (max_cost) des Nutzers. 0 = deaktiviert.
    max_source_cost_eur: float = 140.0
    profit_pct: float = 0.20             # Zusatzgewinn % (auf die Kosten)
    profit_eur: float = 0.0              # Zusatzgewinn als Fixbetrag €
    # Mindestgewinn € (Untergrenze). Auf 4,00 gezogen (Nutzerentscheidung 29.08.2026),
    # damit dieses Modell dieselbe Regel benutzt wie der Upload: "20 %, mindestens 4 €".
    # Die 8,00 stammten aus einer frueheren Regel und liessen das Dashboard dauerhaft
    # einen zu hohen Preis vorschlagen.
    min_profit_eur: float = 4.0
    price_cents: float = 0.95            # Preisendung, z.B. 0.95 -> 149,95 €
    # Ziel-Gewinnmarge (Gewinn/Verkaufspreis) fuer den Preis-Check: liegt die
    # AKTUELLE Marge >= diesem Wert, gilt der Preis als OK (keine Anhebung noetig),
    # auch wenn die Kalkulation theoretisch mehr hergaebe. 0.20 = 20 % (Nutzerregel).
    target_margin_pct: float = 0.20
    # Ziel-Marge fuer die PREIS-EMPFEHLUNG beim HOCHLADEN (Produktideen + neue Produkte,
    # inkl. Variantenpreise) und die Ladenhueter-Senkung. Nutzerregel 02.08.: von 25 % auf
    # 20 % gesenkt – die Preise lagen zu hoch (Gewinn oft ~8 €), wettbewerbsfaehiger ist
    # "20 % ODER mindestens 4 € Gewinn" (der HOEHERE der beiden Boeden gewinnt).
    # Bekleidung von No-Name-Lieferanten gilt als markenlos (Nutzerregel 28.08.2026).
    # Auf false setzen, wenn doch einmal echte Markenbekleidung gelistet wird.
    bekleidung_markenlos: bool = True

    upload_margin_pct: float = 0.20
    # Mindest-EFFEKTIV-Gewinn (€) beim HOCHLADEN: bringt die 25-%-Marge bei sehr guenstigen
    # Artikeln zu wenig €, wird der Preis so weit angehoben, dass mindestens dieser Gewinn
    # bleibt (Nutzerregel 09.07.: "25 % Marge, aber mindestens 4 € effektiv"). Ersetzt die
    # starre 8-€-Grenze fuer neue Produkte -> wettbewerbsfaehigere Preise. Der HOEHERE der
    # beiden Boeden (25-%-Marge vs. 4-€-Gewinn) gewinnt.
    upload_min_profit_eur: float = 4.0
    # DRITTER Boden neben Zielmarge und Mindestgewinn: ein harter
    # Mindestverkaufspreis. Nutzerregel vom 03.09.2026, woertlich: "ich will die
    # tshirts fuer mindestens 19,95 verkaufen ich denke das ist ein fairer preis
    # also gilt nun die regel mindestens 19,95 EUR, und dann noch die zwei
    # anderen regeln mit 4 EUR und 20%".
    #
    # Anders als die beiden anderen Boeden haengt dieser an KEINER Rechnung: er
    # sagt nicht "so viel muss verdient werden", sondern "so guenstig geben wir
    # das Shirt nicht ab". Von den drei Boeden gewinnt immer der hoechste.
    #
    # Bewusst DIESES Feld und kein neues: ``compute_price`` wertet es schon aus,
    # und ueber ``price_from_cny`` haengen alle Rechenwege daran. Ein eigenes
    # Feld nur fuer den Upload-Weg haette die beiden Preismodelle wieder
    # auseinanderlaufen lassen - genau der Fehler vom 29.08.2026, bei dem im
    # Dashboard neben einem Preis von 18,95 ein Vorschlag von 23,95 stand.
    #
    # Bestehende Angebote bleiben unberuehrt: sie tragen ihre eigene Untergrenze
    # am Listing (dort 0), und die hat Vorrang. Ausdruecklicher Nutzerwunsch -
    # "die preise unter 19,95 so lassen bei den entwuerfen um nach einer zeit zu
    # schauen wie sich diese produkte entwickeln".
    min_price_eur: float = 19.95
    max_price_eur: float = 9999.0        # optionale Obergrenze Verkaufspreis
    auto_reprice: bool = True            # Repricing-Job aktiv?
    monitor_cron_hours: int = 6          # Intervall Preis-/Bestands-Monitoring (Stunden)
    # Echte eBay-Pushes im Monitoring (Preis/Menge auf Live-Listings). Default aus,
    # damit Tests/Dev nie real schreiben; in der .env fuer den Betrieb aktivieren.
    monitor_push_real: bool = False
    default_listing_quantity: int = 20   # Standard-Sichtbestand je Artikel/Variante

    # --- AliExpress (offizielle Open-Platform / Dropshipping-API) ---
    aliexpress_app_key: str = ""
    aliexpress_app_secret: str = ""
    # System-Gateway der ds.*-Methoden (IOP). Legacy-Alternative: gw.api.taobao.com/router/rest
    aliexpress_api_base: str = "https://api-sg.aliexpress.com/sync"
    aliexpress_sign_method: str = "sha256"   # "sha256" (HMAC) | "md5" (Secret-umrahmt)
    aliexpress_ship_to: str = "DE"           # Ziel-Land fuer Preise/Versand
    # EUR, damit AliExpress selbst umrechnet - ein eigener Wechselkurs im Code
    # veraltet sonst still. Stand vorher auf CNY, waehrend cny_to_eur_rate von
    # EUR ausging: jedes importierte Produkt bekam dadurch den ~7,8-fachen
    # Einkaufspreis (Testlauf 27.08.2026: T-Shirt fuer 127,09 statt 16,30).
    aliexpress_target_currency: str = "EUR"
    aliexpress_target_language: str = "de"

    @model_validator(mode="after")
    def _waehrung_passt_zum_kurs(self):
        """Verhindert, dass eine fremde Waehrung als Euro verbucht wird.

        ``cny_to_eur_rate`` multipliziert den von AliExpress gemeldeten Preis. Steht
        er auf 1.0, ist das nur richtig, wenn AliExpress bereits Euro liefert. Die
        Kombination "andere Waehrung + Faktor 1.0" ist kein Grenzfall, sondern immer
        ein Fehler - und ein besonders unangenehmer, weil nichts abstuerzt: die
        Zahlen sehen plausibel aus und sind es nicht.

        Beim Testlauf am 27.08.2026 stand die Waehrung auf CNY und der Faktor auf
        1.0. Jedes importierte Produkt bekam den ~7,8-fachen Einkaufspreis; ein
        T-Shirt fuer 16,30 Euro wurde mit 127,09 Euro verbucht und daraus ein
        Verkaufspreis von 237,95 Euro errechnet. Aufgefallen ist es nur, weil die
        Zahl absurd war - bei einem teureren Artikel waere sie durchgegangen.
        """
        waehrung = (self.aliexpress_target_currency or "").strip().upper()
        if waehrung and waehrung != "EUR" and self.cny_to_eur_rate == 1.0:
            raise ValueError(
                f"ALIEXPRESS_TARGET_CURRENCY={waehrung} zusammen mit "
                f"CNY_TO_EUR_RATE=1.0 wuerde {waehrung} als EUR verbuchen. "
                f"Entweder ALIEXPRESS_TARGET_CURRENCY=EUR setzen (empfohlen - dann "
                f"rechnet AliExpress um und im Code veraltet kein Kurs), oder "
                f"CNY_TO_EUR_RATE auf den echten Kurs setzen.")
        return self
    aliexpress_tracking_id: str = ""         # optionale DS-/Affiliate-Tracking-ID
    # Generische Fallback-Telefonnummer (nur Ziffern) fuer AliExpress-Bestellungen,
    # wenn die eBay-Order KEINE Kaeufer-Nummer liefert. Vorwahl kommt aus der Adresse
    # (Default 49). Ohne Nummer lehnt AliExpress die Lieferadresse sonst ab.
    aliexpress_fallback_phone: str = "1600000000"
    # OAuth: ds.*-Methoden brauchen einen access_token (Konto-Autorisierung).
    aliexpress_callback_url: str = ""        # registrierte Redirect-URL der App
    aliexpress_access_token: str = ""
    aliexpress_refresh_token: str = ""
    # Persistenter Token-Speicher (ueberlebt Neustarts; wird beim Auto-Refresh aktualisiert)
    aliexpress_token_file: str = "./data/aliexpress_token.json"

    # --- LLM ---
    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"

    # --- Belegablage ---
    invoice_storage: str = "local"
    invoice_dir: str = "./data/invoices"
    google_drive_credentials_json: str = ""
    # Verkaeufer-Stammdaten fuer § 19-Verkaufsrechnungen (bitte in .env anpassen)
    seller_name: str = ""            # Gewerbe laut Papier -> .env: SELLER_NAME
    seller_address: str = ""         # -> .env: SELLER_ADDRESS
    seller_email: str = ""
    seller_tax_id: str = ""          # Steuernummer/USt-IdNr. (bei § 19 optional)
    invoice_number_prefix: str = "MW"
    # Empfaenger auf der AliExpress-KAUFRECHNUNG. Bewusst NICHT seller_*: dort steht die
    # eBay-Shop-Marke (Druckhelden), waehrend AliExpress an die bei ihnen
    # hinterlegte Firma mit USt-IdNr. adressiert (so steht es auf der Original-Rechnung).
    # Zeilen mit "|" trennen.
    ae_invoice_recipient: str = ""   # -> .env: AE_INVOICE_RECIPIENT (Zeilen mit | trennen)
    # Token fuer den lokalen Belege-Backfill-Client (Playwright laedt echte Belege
    # herunter und laedt sie ueber /invoices/originals/* hoch – Auth per Token statt
    # Login-Session). Leer = Endpoints gesperrt. In .env als BACKFILL_TOKEN setzen.
    backfill_token: str = ""

    # --- Optimierung ---
    opt_weekly_cron_hour: int = 0
    opt_weekly_cron_minute: int = 0
    opt_zero_click_days: int = 30   # 0-Klick-Fenster (Nutzerwunsch: 30 Tage)
    # Nach dem Uebernehmen von Optimierungen wird das Listing so viele Tage aus der
    # Kandidatenliste ausgeblendet (Zeit zum Wirken), danach kommt es zurueck, wenn
    # es immer noch die Klick-Schwelle erfuellt (Nutzerwunsch: 14 Tage).
    opt_hide_days: int = 14
    # Mindestalter, bevor ein (noch nie verkauftes) Listing ueberhaupt als Preis-/Titel-
    # Optimierungs-Kandidat auftaucht (Nutzerwunsch 09.07.: 21 Tage).
    # Nutzerwunsch 08.08.: erst ab 6 Wochen online in die Optimierung —
    # juengere Listings brauchen noch keine Eingriffe.
    opt_min_age_days: int = 42

    # --- KI-Trend-Recherche (Web-Suche via Claude; kostet Token + Web-Suchen) ---
    trend_research_enabled: bool = True    # Auto-Lauf an/aus
    trend_research_weekday: str = "mon"    # (nur falls trend_research_daily=False) Wochentag
    trend_research_daily: bool = True      # True = taeglich (Nutzerwunsch), sonst woechentlich
    trend_research_target: int = 30        # neue Produkte je Trend-Lauf (Nutzerwunsch 15.08.: 30)
    trend_research_terms: int = 16         # wie viele Trend-Keywords je Lauf (mehr = breiter)
    # Lieferzeit-Deckel fuer den TAEGLICHEN Trend-Lauf (Nutzerwunsch 08/2026: nur noch
    # lokale Produkte, d.h. EU-/DE-Lager-Tempo). Manuelle Suchen bleiben frei waehlbar.
    trend_research_max_delivery: int = 7

    # --- Taegliche Store-Entdeckung (Nutzerauftrag 15./16.08.: "such uns jeden Tag
    # 3 Stores raus und fueg sie bei Produktideen ein, ohne zu loeschen") ---
    # Suchseiten sind fuer Server blockiert; STORE-Seiten funktionieren. Kandidaten
    # kommen aus den Store-IDs der vorhandenen Produkt-Ideen (EU-Lager bevorzugt),
    # je Store werden die Bestseller geerntet und als 🏬-Ideen abgelegt (prune-fest).
    store_discovery_daily: bool = True     # Auto-Lauf an/aus (taeglich 05:10 UTC)
    store_discovery_count: int = 3         # wie viele NEUE Stores je Lauf
    store_discovery_per_store: int = 8     # wie viele Ideen je Store maximal
    # Weniger Fokus auf elektrische/elektronische Artikel (Nutzerwunsch 10.07.): hoehere
    # Retourquote + deutsche Compliance-Pflichten (ElektroG/WEEE-Registrierung, Batterie-
    # gesetz BattG). Filtert klar erkennbare Elektronik aus Discover/Trend-Suche.
    avoid_electronics: bool = True

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    def use_mock(self, service: str) -> bool:
        """Effektiver Mock-Status fuer 'llm'|'ebay'|'aliexpress'|'autods'.

        Per-Integration-Override (mock_<service>) hat Vorrang vor use_mocks.
        """
        override = getattr(self, f"mock_{service}", None)
        return self.use_mocks if override is None else override

    @property
    def invoice_path(self) -> Path:
        return Path(self.invoice_dir)


@lru_cache
def get_settings() -> Settings:
    """Gecachte Settings-Instanz (einmal pro Prozess)."""
    return Settings()
