# Versandpartner je Zielland (interne Referenz)

Abgeleitet aus **echten AliExpress-Sendungsnummern** unserer Bestellungen
(Stand 02.07.2026, wächst mit jeder Lieferung). Implementiert in
`order_service.carrier_from_tracking` — die eBay-Tracking-Meldung nutzt
automatisch den richtigen Zusteller-Namen.

## Erkennungs-Systematik

| Muster der Sendungsnummer | Zusteller | Beispiel (echt) |
|---|---|---|
| `003` + 17 Ziffern | **DHL Paket** (Deutschland) | `00340434886280710064` (HTC NE70) |
| `H` + 19 Zeichen | **Hermes** (Deutschland) | – (Regel vom Betreiber bestätigt) |
| UPU-S10: `XX#########YY` | **Landespost des Suffix-Landes** YY | `SE104248586GR` → Hellenic Post (Erazer XF28) |
| 20–26 Ziffern (z. B. `158278…`) | **Cainiao / AliExpress Standard** (übergibt lokal, DE meist an DHL/Post) | `1582788000655337043083` (Vorratsdosen) |

## UPU-Suffix → Landespost (Auszug)

| Suffix | Zusteller | Land |
|---|---|---|
| AT | Österreichische Post | Österreich |
| GR | Hellenic Post (ELTA) | Griechenland |
| NL | PostNL | Niederlande |
| BE | bpost | Belgien |
| FR | La Poste | Frankreich |
| IT | Poste Italiane | Italien |
| ES | Correos | Spanien |
| PL | Poczta Polska | Polen |
| CH | Swiss Post | Schweiz |
| DE | Deutsche Post (Brief/Warenpost) | Deutschland |

Vollständige Tabelle in `order_service._UPU_POST`.

## Beobachtungen aus unseren Sendungen (bisher)

- **Deutschland** (Hauptmarkt): fast ausschließlich **DHL** (`003…`), vereinzelt
  Cainiao-Nummern (`158278…`) bei Choice-Sammellieferungen.
- **Griechenland**: Hellenic Post (`…GR`).
- **Österreich**: Österreichische Post (`…AT`) — vom Betreiber in der
  eBay-Historie beobachtet, Regel entsprechend hinterlegt.

> Pflege: Neue Muster tauchen im Tracking-Sync-Log auf (`carrier=None`-Fälle
> in task_logs prüfen) und werden hier + in `_UPU_POST` ergänzt.
