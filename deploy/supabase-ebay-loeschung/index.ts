// eBay Marketplace Account Deletion - als Supabase Edge Function im Lovable-Projekt.
//
// Warum hier und nicht auf dem PC: eBay schickt bis zu ~1.500 Meldungen am Tag und
// markiert den Endpunkt als ausgefallen, wenn er 24 Stunden nicht antwortet. Der PC
// ist nicht rund um die Uhr an. Diese Funktion nimmt die Meldungen an und hebt sie
// auf; das Programm auf dem PC holt sie ab, sobald es laeuft.
//
// Drei Wege durch dieselbe Adresse:
//   GET  ?challenge_code=...   eBays Pruefung: SHA-256(code + token + endpunkt) als HEX
//   POST (von eBay)            Meldung ROH speichern, erst danach mit 200 quittieren
//   GET  ?abholen=1            (Header x-abhol-token) offene Meldungen fuer den PC
//   POST ?quittieren=1         (Header x-abhol-token) {"ids":[...]} verarbeitete loeschen
//
// Geheimwerte (in Lovable/Supabase als Secrets, NICHT hier im Code):
//   EBAY_VERIFICATION_TOKEN   derselbe Wert wie auf developer.ebay.com, 32-80 Zeichen
//   EBAY_ENDPOINT_URL         exakt die bei eBay eingetragene Adresse dieser Funktion
//   EBAY_ABHOL_TOKEN          nur zwischen dieser Funktion und dem PC-Programm
// SUPABASE_URL und SUPABASE_SERVICE_ROLE_KEY stellt Supabase selbst bereit.
//
// Die Funktion muss OHNE Supabase-Anmeldung erreichbar sein (verify_jwt = false):
// eBay schickt keinen Supabase-Schluessel mit.

import { createClient } from "npm:@supabase/supabase-js@2";

const TOKEN = Deno.env.get("EBAY_VERIFICATION_TOKEN") ?? "";
const ENDPUNKT = Deno.env.get("EBAY_ENDPOINT_URL") ?? "";
const ABHOL_TOKEN = Deno.env.get("EBAY_ABHOL_TOKEN") ?? "";
const TABELLE = "ebay_loeschmeldungen";
const HOECHSTENS_JE_ABHOLUNG = 500;

// Neuere Supabase-Projekte nutzen SUPABASE_SECRET_KEY; dort ist
// SUPABASE_SERVICE_ROLE_KEY zwar noch gesetzt, aber abgeschaltet. Mit dem falschen
// Schluessel scheitert jedes Speichern - und eBay bekaeme 500 statt 200.
const DIENST_SCHLUESSEL =
  Deno.env.get("SUPABASE_SECRET_KEY") || Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || "";

const db = createClient(Deno.env.get("SUPABASE_URL") ?? "", DIENST_SCHLUESSEL, {
  auth: { persistSession: false },
});

function json(inhalt: unknown, status = 200): Response {
  return new Response(JSON.stringify(inhalt), {
    status,
    headers: { "content-type": "application/json" },
  });
}

// Vergleich in konstanter Zeit - sonst verraet die Antwortdauer den Abhol-Token
// Zeichen fuer Zeichen.
function gleich(a: string, b: string): boolean {
  if (!a || !b || a.length !== b.length) return false;
  let unterschied = 0;
  for (let i = 0; i < a.length; i++) unterschied |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return unterschied === 0;
}

async function sha256Hex(text: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

Deno.serve(async (req: Request): Promise<Response> => {
  const url = new URL(req.url);
  const darfAbholen = gleich(req.headers.get("x-abhol-token") ?? "", ABHOL_TOKEN);

  // --- PC holt offene Meldungen ab ---------------------------------------------
  if (req.method === "GET" && url.searchParams.has("abholen")) {
    if (!darfAbholen) return json({ error: "nicht berechtigt" }, 401);
    const { data, error } = await db
      .from(TABELLE)
      .select("id, eingegangen_am, signatur, roh")
      .order("id", { ascending: true })
      .limit(HOECHSTENS_JE_ABHOLUNG);
    if (error) return json({ error: error.message }, 500);
    return json({ meldungen: data ?? [] });
  }

  // --- PC meldet: verarbeitet, darf weg ----------------------------------------
  if (req.method === "POST" && url.searchParams.has("quittieren")) {
    if (!darfAbholen) return json({ error: "nicht berechtigt" }, 401);
    const koerper = await req.json().catch(() => ({}));
    const ids = Array.isArray(koerper?.ids) ? koerper.ids.filter(Number.isInteger) : [];
    if (ids.length === 0) return json({ geloescht: 0 });
    const { error } = await db.from(TABELLE).delete().in("id", ids);
    if (error) return json({ error: error.message }, 500);
    return json({ geloescht: ids.length });
  }

  // --- eBays Pruefung ----------------------------------------------------------
  if (req.method === "GET") {
    const code = url.searchParams.get("challenge_code");
    if (!code) return json({ error: "missing challenge_code" }, 400);
    // Fehlt ein Geheimwert, lieber klar scheitern als einen falschen Hash liefern -
    // eBay sagt sonst nur "validation failed", ohne Grund.
    if (!TOKEN || !ENDPUNKT) {
      return json({ error: "EBAY_VERIFICATION_TOKEN oder EBAY_ENDPOINT_URL fehlt" }, 500);
    }
    return json({ challengeResponse: await sha256Hex(code + TOKEN + ENDPUNKT) });
  }

  // --- eBays Meldung -----------------------------------------------------------
  if (req.method === "POST") {
    // ROH speichern, nicht als JSON: Die Signatur gilt fuer genau diese Bytes. Eine
    // Datenbank, die JSON umsortiert, wuerde jede spaetere Pruefung scheitern lassen.
    const roh = await req.text();
    const { error } = await db.from(TABELLE).insert({
      roh,
      signatur: req.headers.get("x-ebay-signature"),
    });
    // Nur quittieren, was wirklich gespeichert ist. Ein 200 ohne Speichern hiesse:
    // eBay haelt die Loeschung fuer zugestellt, und sie geht fuer immer verloren.
    // Ein Fehler laesst eBay es spaeter erneut versuchen.
    if (error) return json({ error: "nicht gespeichert" }, 500);
    return json({ status: "acknowledged" });
  }

  return json({ error: "Methode nicht erlaubt" }, 405);
});
