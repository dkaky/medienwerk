# Druckhelden Dashboard — Architecture

Source: multi-agent analysis of `ebay-automation` (mature FastAPI + dashboard) and
`pod-shop` (this POD CLI). Goal: one dashboard for the whole business, reusing as
much of `ebay-automation` as sensible, multi-shop / multi-provider ready, with a
second AliExpress **poster / wall-art** dropshipping stream.

## Big picture
- **Backend**: new FastAPI app that wraps the existing `pod-shop` library as services,
  plus ported infrastructure from `ebay-automation` (DB, config, auth, scheduler,
  retry, logging, finance/invoice/analytics). Lives in this repo: `src/pod/` stays the
  domain core; add `app/` (backend) + `web/` (frontend) + `alembic/`.
- **Frontend**: build fresh (small SPA). Do NOT gut the 232 KB single-file eBay
  dashboard — it is one AliExpress-arbitrage screen and fights the new model. Reference
  it only for UX patterns (KPI cards, activity feed, propose-only approve/reject).
- **Two streams, one dashboard**: (A) POD — AI design -> review/approve -> Printify
  product -> fan out to marketplaces; (B) AliExpress poster resale -> list -> supplier
  fulfillment. Separated by a `stream` discriminator + mutually exclusive FKs; they
  share Shop / Listing / Order / Finance / Auth but never share product logic.

## Data model (entities)
- **Shop** — a selling account on a marketplace (name, marketplace, credentials_ref, active).
- **Design** — POD only (theme, prompt, provider, overlay, image/thumbnail paths, status, reason).
- **Product** — producible item from a Design at a provider (design_id, provider, product_type, external id, base_cost, status).
- **PosterSource** — AliExpress stream only (supplier, supplier_product_id, url, title, images, price, ship, status).
- **Listing** — UNIFIED across streams + marketplaces (shop_id, stream, product_id? , poster_source_id?, marketplace, external_id, title, price, status). Exactly one of product/poster set.
- **Order** — UNIFIED (shop_id, listing_id, stream, marketplace_order_id, buyer, sale_price, real fee, cogs, status, fulfillment_ref, tracking).
- **Invoice** — §19-compliant (order_id, type, number, file_path SHA256-dedup, period, amount).
- **Job** — background-work progress (type, status, per-item progress JSON, reference, error).
- **TaskLog** — audit feed (ported as-is).

## Reuse map (highlights)
| Source | Action | Note |
|---|---|---|
| pod-shop pipeline/printify/pricing/generation/safety | **port-as-is** | already DI'd + pure -> wrap as services |
| pod-shop storage/config/themes | **adapt** | WAL+pagination+count; Pydantic at edge |
| pod-shop cli.py | **reference** | 1:1 blueprint for the endpoint set |
| ebay-automation database/retry/logging/common | **port-as-is** | domain-agnostic infra |
| ebay-automation main/config/auth/scheduler | **adapt** | strip eBay bits, keep HMAC cookie gate |
| ebay-automation integrations/ebay.py | **port-as-is** | OAuth refresh, offer create+publish, order poll, IPN — unblocks eBay |
| ebay-automation integrations/aliexpress* | **adapt** | money-safe supplier -> poster stream |
| ebay-automation llm/native_listing/storage/spec_filter | **adapt** | LLM metadata, MarketplaceBackend seam, thumbnails, content compliance |
| ebay-automation finance/invoice/analytics/deletion/publish_queue | **adapt** | §19 report, invoices, KPIs, GDPR, restart-safe queue |
| ebay-automation autods / monitoring / listing_match | **skip** | arbitrage-only, POD has no such need |

## Multi-shop / multi-provider
- **Shop** table + **MarketplaceBackend ABC** (ported from `native_listing.py` seam):
  one design/product -> N Listings (shop x marketplace). Adding Spreadshirt/Redbubble =
  one new backend + a Shop row + config; no change to design/review/finance code.
- Two provider layers: **production** (where made: Printify now) vs **selling
  marketplace** (where listed: eBay now). Spreadshirt/Redbubble are both at once.

## Phased plan (each phase shippable)
1. **POD backend + Design Review** (no external accounts, mock provider): FastAPI wrapping
   pod-shop, Job worker, design endpoints + image/thumbnail serving, Design Review UI
   (gallery, preview, one-click approve/reject, generate panel w/ progress, live IP-check),
   Overview shell. Tests >=80%.
2. **Products + Printify + Pricing**: approved designs -> Printify drafts, cost/margin
   preview, Products view.
3. **Multi-shop Listings + eBay publish**: Shop table + MarketplaceBackend, EbayBackend
   from ported ebay.py, one-time OAuth consent, Listings matrix + retry, Spreadshirt/
   Redbubble stubs.
4. **Orders + Fulfillment + Finance/§19**: eBay order ingest, Printify fulfillment,
   invoices, §19 report/CSV/ZIP, analytics KPIs, Orders + Finance views.
5. **AliExpress poster stream**: poster_service (scrape, money-safe order, tracking),
   PosterSource, stream discriminator through listings/orders/finance, Poster Sourcing view.

## Key risks
- eBay accounts not yet fully provisioned (OAuth consent + refresh token + signing key) —
  Phases 1-2 need no external creds, so work is not blocked.
- SQLite write-lock: never hold a write tx across network calls (keep connect-per-op, add WAL+busy_timeout).
- Single-worker only (in-process scheduler/locks) — enforce one instance.
- Long blocking calls (image gen, Printify bulk) must run in the Job worker threadpool, never on the event loop.
- Money-safety on both streams: idempotency keys, explicit-click gating, never auto-retry uncertain order outcomes.
- Large PNGs -> thumbnailing + route now; secrets in a store, never plaintext in git.

## Decisions to confirm (defaults in bold)
- Frontend: **React + TypeScript + Vite** (recommended for image-heavy review) vs vanilla ESM (no build step).
- DB: **SQLite now** (matches pod-shop) + Alembic, migrate to Postgres/Neon later, vs Postgres from day 1.
- Poster supplier: **AliExpress** (near-drop-in from ebay-automation).
- Location: **build inside this pod-shop repo** (`app/` + `web/`), pod-shop stays the domain library.
