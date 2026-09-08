/**
 * eBay Marketplace Account Deletion / Closure Notification — Cloudflare Worker.
 *
 * Kostenloser, dauerhaft erreichbarer HTTPS-Endpunkt, der eBays Pflicht-
 * Validierung besteht und so das Production-Keyset freischaltet.
 *
 * Worker-Variablen (im Cloudflare-Dashboard unter Settings -> Variables setzen):
 *   VERIFICATION_TOKEN  Ein selbstgewaehlter String, 32-80 Zeichen. EXAKT derselbe
 *                       Wert muss spaeter auf developer.ebay.com eingetragen werden.
 *   ENDPOINT_URL        Die exakte oeffentliche URL dieses Workers, OHNE abschliessenden
 *                       Slash, z. B. https://ebay-deletion.<dein-subdomain>.workers.dev
 *                       (muss exakt dem auf eBay registrierten Wert entsprechen).
 */
export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // 1) GET-Challenge: eBay prueft den Endpunkt -> SHA-256(code+token+url) als HEX.
    if (request.method === "GET") {
      const challengeCode = url.searchParams.get("challenge_code");
      if (!challengeCode) {
        return json({ error: "missing challenge_code" }, 400);
      }
      const data = challengeCode + env.VERIFICATION_TOKEN + env.ENDPOINT_URL;
      const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(data));
      const hex = [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
      return json({ challengeResponse: hex }, 200);
    }

    // 2) POST-Notification: bestaetigen (Datenloeschung erfolgt in deinem Programm).
    if (request.method === "POST") {
      return json({ status: "acknowledged" }, 200);
    }

    return new Response("ok", { status: 200 });
  },
};

function json(obj, status) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });
}
