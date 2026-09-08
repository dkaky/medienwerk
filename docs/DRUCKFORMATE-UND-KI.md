# Druckformate und KI-Modelle — Recherche 23.08.2026

> Ergebnis einer Netzrecherche (Printify-Doku, GitHub, Foren) plus Prüfung des
> Altbestands. Grundlage für die Bilderzeugung im Studio-Trakt.

## Kurzantwort vorab

**Ein einziges Universalmaß gibt es nicht.** Der Grund ist rein geometrisch: Das Seitenverhältnis reicht von **2,14 : 1 (Tasse, quer)** bis **0,56 : 1 (Handyhülle, hochkant)** — Faktor ~3,8. Was Profis stattdessen machen: **eine hochauflösende Masterdatei + pro Produktgruppe ein eigenes Layout**, nicht ein Bild mehrfach zuschneiden.

---

## 1. Gibt es ein Universalmaß? Nein — und zwar aus vier Gründen

| Produkt | Pixel | Seitenverhältnis | Ausrichtung |
|---|---|---|---|
| T-Shirt (Front, DTG) | 4500 × 5400 | 5:6 = **0,83** | hoch |
| Hoodie | 4800 × 6000 | 4:5 = **0,80** | hoch |
| Poster 18×24" | 5400 × 7200 | 3:4 = **0,75** | hoch |
| Poster 24×36" | 7200 × 10800 | 2:3 = **0,67** | hoch |
| Tasse 11 oz | 2475 × 1155 | **2,14** | **quer** |
| Tasse 15 oz | 2790 × 1365 | **2,04** | **quer** |
| Handyhülle | ~1242 × 2208 | 9:16 = **0,56** | **extrem hoch** |
| Tote Bag | 4200 × 4200 | **1,00** | quadratisch |
| Kissen 18×18" | 5400 × 5400 | **1,00** | quadratisch |

**Warum kein gemeinsamer Nenner:**

1. **Geometrie.** Ein 5:6-Shirtmotiv in ein 2,14:1-Tassenfeld gezogen ist entweder verzerrt oder zu ~65 % leer.
2. **Druckverfahren.** Printful verlangt bei Textil mindestens **150 DPI**, bei Kleinteilen wie Tassen und Hüllen **300 DPI** — kleine Details „kommen bei Tassen verschwommen heraus" (Printify Design Guide).
3. **Hintergrund-Logik ist gegensätzlich.** Textil (DTG) braucht **transparentes PNG** ohne Farbverläufe, die in Transparenz auslaufen. Tasse/Poster brauchen **Full Bleed** — randlos gefüllt bis in die Beschnittzone. Dieselbe Datei kann nicht beides sein.
4. **Physische Sperrzonen.** Tasse: Henkel und sichtbare Wickelnaht. Shirt: 2 Zoll unter Frontkragen, 3 Zoll unter Rückenkragen freihalten. Poster: keine.

**Die eine Ausnahme:** Marktplätze wie **Redbubble, TeePublic, Zazzle** nehmen *einen* Upload und fitten ihn automatisch auf alles. Redbubbles Empfehlung für „ein Bild für alle Produkte" ist **7632 × 6480 px** (deckt sogar den King-Size-Bettbezug ab). Das Ergebnis ist aber sichtbar Kompromiss — Redbubble selbst wirbt mit dem Artikel *„4 Reasons Why You Should Upload Different Images for Each Product"*.

**Printify und Printful arbeiten dagegen strikt per-product**: jede Produktart nimmt ihre eigene Datei in ihrer eigenen Größe. Für dieses Projekt (Printify) ist die „Ein-Upload"-Variante also gar nicht verfügbar.

---

## 2. Vorgehen der Profis: Master + Ableitung, aber kein blindes Zuschneiden

Der Konsens über Vendor-Docs, Tool-Anbieter und Seller-Blogs hinweg:

**Schritt 1 — Master anlegen, immer größer als das größte Ziel.**
Merch Titans: „create your master file at 5000 × 5500 px at 300 DPI" und die eiserne Regel: **„Scaling up from a smaller file destroys quality."** Printify sagt dasselbe: automatisch runterskalieren ist unkritisch, hochskalieren nicht. Wer auch Poster bedienen will, braucht mehr: 24×36" verlangt 7200 × 10800 px.

**Schritt 2 — Master ist eine Komposition, kein Flachbild.**
Der entscheidende Punkt, den man in Vendor-Docs kaum, in Praktiker-Quellen dagegen überall findet: Das Master ist eine **Ebenen-Datei (PSD/SVG) mit trennbaren Elementen** — Headline, Subline, Illustration, Ornamente. Umformatieren heißt dann **neu anordnen**, nicht croppen.

**Schritt 3 — Pro Produktgruppe eine eigene Anordnung.**
HansCo formuliert es am klarsten: *„Keep the concept and visual identity, but redesign the composition."* Konkret dort: Shirt bekommt kleines Brust-Emblem plus große Rückenillustration; Sticker muss „auf viel kleinerer Skala vollständig wirken", nicht die verkleinerte Version des Großformats sein.

**Was Profis *nicht* machen:** verzerren (stretch). Was sie widerwillig machen: croppen. ResizeFlow rät zu „crop them non-destructively" statt Strecken — Crop ist die Notlösung, nicht der Idealweg. In Praktikerdiskussionen tauchen Vielverkäufer auf, die tatsächlich einfach zuschneiden und abgeschnittene Elemente in Kauf nehmen; das ist eine Durchsatz-Entscheidung, keine Qualitätsempfehlung.

**Praktische Gruppierung — es reichen 3 Layouts, nicht 5:**

| Layout | Bedient | Verhältnis |
|---|---|---|
| **A: Hochkant** | Shirt, Hoodie, Poster, Handyhülle | 0,56–0,83 |
| **B: Querformat** | Tasse (11 oz + 15 oz) | ~2,1 |
| **C: Quadrat** | Tote, Kissen, Sticker | 1,0 |

Layout A deckt vier Produkte mit *einer* Neuanordnung ab, weil 0,83 bis 0,67 nah beieinander liegen — hier reicht Contain-Skalierung mit transparentem Rand. Nur die Handyhülle (0,56) braucht meist noch eine Straffung.

---

## 3. Konkrete Maße

**Textil**

| Platzierung | Zoll | Pixel @ 300 DPI |
|---|---|---|
| Full Front Erwachsene | 12 × 16 | 3600 × 4800 |
| Upload-Standard (Printify/Printful/Amazon) | — | **4500 × 5400** |
| Ladies Fit | 10 × 13 | 3000 × 3900 |
| Brust links | 3–5 | 900–1500 quadratisch |
| Rücken | 12 × 13 | 3600 × 3900 |
| Ärmel kurz | 3,5 × 3,5 | 1050 × 1050 |
| All-Over-Print (Printful) | — | 8640 × 5460 @150 DPI |

**Tassen**

- 11 oz: **2475 × 1155 px @ 300 DPI** (= 8,25 × 3,85 Zoll) — bestätigt von Printify Knowledge Hub *und* Printful-Spezifikation.
- 15 oz: **2790 × 1365 px**
- Printfuls Druckfläche in Zoll: 9 × 3,5" (23 × 9 cm)
- ⚠️ **Widerspruch in Sekundärquellen:** ResizeFlow nennt 2700 × 1095 bzw. 2700 × 3600. Das deckt sich nicht mit den Herstellerangaben. **Immer das Template beim jeweiligen Print Provider ziehen** — Printify hat pro Provider unterschiedliche Tassenmaße.

**Poster (× 300)**

| Größe | Verhältnis | Pixel |
|---|---|---|
| 16 × 20" | 4:5 | 4800 × 6000 |
| 18 × 24" | 3:4 | 5400 × 7200 |
| 24 × 36" | 2:3 | 7200 × 10800 |

Printify-Poster laufen in **2:3, 3:4, 4:5 und 11:14**. Faustregel: Zoll × 300.

**Handyhülle:** ~1242 × 2208 px @ 300 DPI. Stark gerätespezifisch, Template ziehen.

**Plattform-Limits**

| Plattform | Upload | Format | Max |
|---|---|---|---|
| Amazon Merch | 4500 × 5400 | nur PNG, transparent, sRGB | 25 MB |
| Printify | 4500 × 5400 | PNG/JPG/SVG | 100 MB / SVG 20 MB, max 20 000 Pfade |
| Printful | 4500 × 5400 | PNG bevorzugt | 200 MB |
| Redbubble | min. 2400 × 3200, empf. 4500 × 5400, alles: 7632 × 6480 | PNG/JPG/GIF | 300 MB |
| TeePublic | 5000 × 5500 | PNG | — |

**Farbraum:** durchgehend **sRGB (IEC61966-2.1)**. Printify konvertiert CMYK-JPEGs ohnehin nach RGB.

---

## 4. Quer vs. hoch — vier Strategien

**a) Contain + transparente/gefüllte Ränder** (billigste Automatisierung)
Motiv proportionswahrend einpassen, Rest auffüllen. Bei Textil transparent, bei Tasse/Poster mit Hintergrundfarbe füllen. Nachteil bei der Tasse: das Motiv wird winzig, weil die Höhe limitiert.

**b) Zwei-Zonen-Layout für die Tasse** (die eigentliche Profi-Lösung)
Die Tasse hat **zwei Ansichtsseiten**, links und rechts vom Henkel. Statt ein Hochformat zu quetschen: Motiv links, Text rechts. Der Bereich unter dem Henkel wird bewusst als Pause genutzt. Praxisregeln:
- Keine wichtige Typo direkt unter den Henkel.
- Beide Mockups prüfen — Rechts- und Linkshänder.
- Große, lesbare Type, weil ein Teil des Designs immer wegkrümmt.

**c) Textumbruch umkehren.** Ein dreizeiliger Spruch im Hochformat wird für die Tasse ein- oder zweizeilig. Reines Umbruch-Rearrangement, keine neue Illustration.

**d) Nahtloses Wickelmuster.** Printify: „Fill the entire wrap — edge to edge." Aber im Design Guide steht die Einschränkung: **die Naht bleibt auch bei Full Bleed oft sichtbar.** Also Full Bleed ja, aber nie ein durchgehendes Schlüsselelement über die Wickelkante legen.

**Für die Handyhülle** (0,56 — noch schmaler als das Shirt): Motiv vertikal strecken heißt hier nicht skalieren, sondern **Elemente vertikal stapeln statt nebeneinander**.

---

## 5. Werkzeuge und Skripte

**Kommerziell / No-Code**

| Tool | Was es tut |
|---|---|
| **Kittl** | Vorgeladene POD-Presets, setzen die Leinwand automatisch auf Printify-/Merch-Maße |
| **Canva Magic Switch** | Design auf abweichende Zielmaße umschalten, für Provider-Wechsel gedacht |
| **RatioReady** | Batch: ein Master → Ordner pro Produkttyp mit exakten Printful-Specs, setzt 300-DPI-Metadaten, erhält Transparenz |
| **ResizeFlow** | Batch-Zuschnitt/Upscale auf Printify- und Printful-Vorgaben |
| **Photoshop Artboards** | Ein Dokument, ein Artboard pro Produktmaß, Batch-Export benennt Dateien nach Artboard |
| **Merch Informer Designer** | Browserbasiertes Bulk-Tool für skalierte Design-Varianten |

⚠️ RatioReady und ResizeFlow sind Marketing-Sites für die eigenen Resizer. Die Maße stimmen weitgehend mit Herstellerangaben überein, bei der Tasse aber nicht. Nicht als Primärquelle behandeln.

**Open Source (GitHub)**

| Repo | Sterne | Zweck |
|---|---|---|
| `CTDave001/automated_mockups` | 69 | Python/Pillow+OpenCV. Legt Designs per Farb-Platzhaltererkennung auf Templates. **9 Alignments, 4 Scale-Modi: fit / fill / stretch / none.** Config als JSON. Achtung: erzeugt **Mockups**, keine Druckdateien |
| `lawrencemq/printipy` (PyPI) | — | Printify-API-Client für Python 3.9–3.12 |
| `yoest/easyidea` | 12 | Vereinfacht das Erstellen mehrerer Designs für Shirts, Tassen etc. |
| `kylemillerbuilds/printify-publish-queue` | 2 | Gehärteter Publish-Flow für die Printify-API |
| `zyadhajaji/threadforge` | 6 | Open-Source-Mockup-Plattform, TypeScript |
| `IncomeStreamSurfer/print_on_demand_printify_automation` | — | Stable Diffusion → Printify → Shopify, inklusive SEO |

**Ein echter Umrechner ist in keinem dieser Repos enthalten.** Die Bausteine dafür liegen in Pillow selbst:

- `ImageOps.contain(img, size)` — passt ein, ohne zu beschneiden → Strategie (a)
- `ImageOps.fit(img, size, centering=)` — beschneidet mittig auf Zielverhältnis
- `ImageOps.pad(img, size, color=)` — contain plus Randfüllung in einem Aufruf
- PyPI `pil_resize_aspect_ratio` mit `resize_keep_aspect_ratio()` und `paste_and_fit()`

Ein Umrechner ist also ~30 Zeilen: Zielformate als Dict, `ImageOps.pad` mit transparentem oder gefülltem Rand, DPI-Metadaten setzen. Der einzige Fallstrick ist, dass **`fit` bei der Tasse ~65 % des Motivs wegschneidet** — deshalb dort `pad` statt `fit`, oder besser gleich ein eigenes Querlayout.

---

## Übertragung auf dieses Projekt

Da `src\pod\` bereits Printify mit Fit-Scale-Placement fährt und die Zwei-Spuren-Regel (gpt-image-1 für Text, Flux für Illustration) gilt, wäre die saubere Konsequenz:

- **Zwei Generierungsläufe pro Konzept statt einem**: ein Hochformat-Prompt (bedient Shirt/Hoodie/Poster/Hülle über `pad`) und ein Querformat-Prompt für die Tasse. Ein einziger Lauf plus Zuschnitt reicht geometrisch nicht.
- **Bei Textdesigns niemals automatisch auf Tassenformat croppen** — der Umbruch muss neu gesetzt werden, sonst reißt die Zeile ab. Das betrifft genau die gpt-image-1-Spur.
- **Master ≥ 7200 px lange Kante** anlegen, wenn Poster im Katalog bleiben sollen; 4500 × 5400 reicht nur für Textil.
- **Tassen-Templates pro Print Provider ziehen**, nicht global 2475 × 1155 hartkodieren.

---

## Einschränkung zur Recherche

**Reddit war nicht direkt abrufbar** — `reddit.com` blockiert den Crawler dieser Umgebung (HTTP-Fehler bzw. Domain-Sperre). Die Reddit-bezogenen Aussagen oben stammen aus Suchmaschinen-Zusammenfassungen, nicht aus gelesenen Threads. Ebenso lieferten `help.printify.com`, `help.redbubble.com` und `teepublic.zendesk.com` **403 Forbidden**; deren Inhalte kamen über Printifys öffentlichen Design Guide, den Knowledge Hub und Sekundärquellen. Die Zahlen für Shirt, Tasse und Poster sind dagegen über mindestens zwei unabhängige Quellen bestätigt — außer der oben markierte Tassen-Widerspruch.

## Quellen

- [Printify — Must-read design guide](https://printify.com/guide/design-guide/)
- [Printify — Print on Demand Mugs: Design & Sales Guide](https://printify.com/knowledge-hub/how-to-design-and-sell-pod-mugs/)
- [Printify — T-shirt design placement guide 2026](https://printify.com/blog/t-shirt-design-placement-guide/)
- [Printful — How to prepare the perfect print file](https://www.printful.com/blog/everything-you-need-to-know-to-prepare-the-perfect-printfile)
- [Printful — What I wish I knew about POD: Expert tips](https://www.printful.com/blog/launching-a-pod-store-expert-tips)
- [Merch Titans — T-Shirt Design Dimensions: Exact Sizes for Every POD Platform](https://merchtitans.com/blog/t-shirt-design-dimensions-guide)
- [Ratio Ready — Printful Upload Specs: Pixel Dimensions & DPI by Product](https://ratioready.com/print-ready/printful)
- [Ratio Ready — How to Prepare Files for Printify](https://ratioready.com/guides/how-to-prepare-files-for-printify)
- [ResizeFlow — Ultimate Sizing Guide for Printify & Printful](https://resizeflow.com/blog/ultimate-sizing-guide-printify-printful-products)
- [HansCo — POD Design Ideas for T-Shirts, Mugs, Tote Bags, Stickers](https://hanscostudio.com/pod-design-ideas/)
- [Kittl — Placement t-shirt design size chart: 15 print rules](https://www.kittl.com/blogs/placement-t-shirt-design-size-chart-pod/)
- [Kittl — Print on Demand solution / POD presets](https://www.kittl.com/solutions/print-on-demand)
- [Redbubble Blog — 4 Reasons Why You Should Upload Different Images for Each Product](https://blog.redbubble.com/2015/01/4-reasons-why-you-should-upload-different-images-for-each-product/)
- [Redbubble Blog — Bulk Editing Tools to Add and Configure Products](https://blog.redbubble.com/2021/10/using-the-bulk-editing-tools-to-add-and-configure-products/)
- [Icons8 — Guide to Redbubble image size requirements](https://icons8.com/blog/articles/redbubble-image-size/)
- [TeePublic — Designing for Specific Products](https://www.teepublic.com/blog/designing-for-specific-product-dimensions)
- [GitHub Topic — print-on-demand](https://github.com/topics/print-on-demand)
- [GitHub — CTDave001/automated_mockups](https://github.com/CTDave001/automated_mockups)
- [GitHub — lawrencemq/printipy](https://github.com/lawrencemq/printipy) / [PyPI printipy](https://pypi.org/project/printipy)
- [PythonInformer — Image resizing recipes in Pillow (contain/fit/pad)](https://www.pythoninformer.com/python-libraries/pillow/imageops-resizing/)
- [PyPI — pil-resize-aspect-ratio](https://pypi.org/project/pil-resize-aspect-ratio/)
- [Merch Informer — Create Scaled Design Variations](https://merchinformer.com/create-scaled-design-variations-with-the-new-merch-informer-designer-update/)
- [Canva Help — Using Canva to create products for sale](https://www.canva.com/help/using-canva-to-create-products-for-sale/)
- [Printkeg — Print Aspect Ratios Explained](https://www.printkeg.com/blogs/tips/print-aspect-ratios-guide)