-- Zwischenlager fuer eBay-Loeschmeldungen, bis das PC-Programm sie abholt.
--
-- Bewusst nur ROH-Text statt jsonb: Die eBay-Signatur gilt fuer genau die Bytes, die
-- eBay geschickt hat. jsonb sortiert Schluessel um und entfernt Leerzeichen - danach
-- liesse sich keine Meldung mehr pruefen.
--
-- Verarbeitete Meldungen loescht das PC-Programm ueber die Funktion. Bei bis zu
-- ~1.500 Meldungen am Tag bleibt die Tabelle damit klein.

create table if not exists public.ebay_loeschmeldungen (
  id bigint generated always as identity primary key,
  eingegangen_am timestamptz not null default now(),
  signatur text,
  roh text not null
);

create index if not exists ebay_loeschmeldungen_eingang
  on public.ebay_loeschmeldungen (eingegangen_am);

-- Zeilenschutz AN und KEINE Richtlinie: Die Website (anonymer Schluessel) sieht und
-- schreibt hier nichts. Nur die Edge Function mit der Service-Rolle darf ran.
alter table public.ebay_loeschmeldungen enable row level security;
