// Freya Bank statement-debt mock. Serves the synthetic answers in data.json exactly.
import data from "./data.json" with { type: "json" };

const CORS = { "Access-Control-Allow-Origin": "*" };
const JSON_HEADERS = { ...CORS, "Content-Type": "application/json; charset=utf-8" };

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });

const guid = () => [...crypto.getRandomValues(new Uint8Array(16))]
  .map((b) => b.toString(16).padStart(2, "0")).join("");

function errorEnvelope(code, message, type, rootCause) {
  const g = guid();
  return {
    result: {
      guid: g, returnCode: code, messageText: message, code, message, type,
      details: { guid: g, rootCause, errorOrigin: "statement-mock" },
    },
  };
}

const isTemplate = (v) => typeof v === "string" &&
  (v.trim().startsWith("{{") || v.trim().startsWith("%7B%7B"));
const norm = (v) => (v === null || v === undefined ? "" : String(v).trim());

const customers = data.customers;
const byNo = new Map(customers.map((c) => [norm(c.customerNo), c]));

// service -> { key: "customerNo" | "cardNumber", week?: bool, nf: 404 info or null }
const SERVICES = {
  "cards.list": { key: "customerNo", nf: ["FB_IVR_CHANNEL_102", "Müşteriye ait geçerli kart bilgisi bulunamadı.", "WARNING", "NotFoundException"] },
  "cards.balance": { key: "cardNumber", nf: ["FB_IVR_CHANNEL_120", "Kart bakiye servisi hatası: kart bulunamadı", "WARNING", "NotFoundException"] },
  "statement.week.get": { key: "cardNumber", nf: ["FB_IVR_CHANNEL_121", "Kart bilgisi servisi hatası: kart bulunamadı", "WARNING", "NotFoundException"] },
  "statement.week.decide": { key: "cardNumber", week: true, nf: null },
  "statement.week.change": { key: "cardNumber", week: true, nf: null },
  "contact.email.get": { key: "customerNo", nf: null },
  "statement.pdf.send": { key: "customerNo", nf: ["FB_IVR_CHANNEL_107", "Pdf dokümanı bulunamadı", "WARNING", "com.freyabank.ivrgateway.ivrchannel.rest.exception.NotFoundException"] },
};

const FIELD_LABEL = { customerNo: "customer number", cardNumber: "card number", newStatementWeek: "new statement week" };

const bad = (field) => json(errorEnvelope("INVALID_ARGUMENTS_ERROR", `${FIELD_LABEL[field]} cannot be empty`, "ERROR", "InvalidArgumentsException"), 400);

function notFound(def, what) {
  if (def.nf) {
    const [code, msg, type, root] = def.nf;
    return json(errorEnvelope(code, msg, type, root), 404);
  }
  return json(errorEnvelope("INVALID_ARGUMENTS_ERROR", `${what} not found`, "ERROR", "InvalidArgumentsException"), 400);
}

function lookup(service, def, d) {
  const idRaw = d[def.key];
  const tpl = isTemplate(idRaw);
  const id = norm(idRaw);
  let weekRaw = def.week ? d.newStatementWeek : undefined;
  const weekTpl = isTemplate(weekRaw);
  const week = norm(weekRaw);

  if (tpl || (def.week && weekTpl)) {
    // Test API Request button: serve the first customer's valid record.
    for (const c of customers) {
      const e = c.services[service];
      if (!e) continue;
      if (def.key === "customerNo") { if (e.response && e.http === 200) return e; continue; }
      for (const card of Object.keys(e)) {
        const sub = e[card];
        if (def.week) {
          if (tpl || card === id) {
            const w = weekTpl ? Object.keys(sub)[0] : week;
            if (sub[w] && sub[w].http === 200) return sub[w];
          }
        } else if (sub.http === 200) return sub;
      }
    }
    return null;
  }

  if (def.key === "customerNo") {
    const c = byNo.get(id);
    return c && c.services[service] ? c.services[service] : null;
  }
  for (const c of customers) {
    const e = c.services[service];
    if (e && Object.prototype.hasOwnProperty.call(e, id)) {
      const sub = e[id];
      return def.week ? (sub[week] || null) : sub;
    }
  }
  return null;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function handle(service, request, env) {
  let body = null;
  try { body = await request.json(); } catch { body = null; }
  const d = (body && typeof body.data === "object" && body.data) || {};
  const def = SERVICES[service];

  if (!norm(d[def.key])) return bad(def.key);
  if (def.week && !norm(d.newStatementWeek)) return bad("newStatementWeek");

  const entry = lookup(service, def, d);
  if (!entry) return notFound(def, FIELD_LABEL[def.key]);

  if (entry.delaySeconds) {
    const ms = env && env.DELAY_MS_OVERRIDE !== undefined && env.DELAY_MS_OVERRIDE !== ""
      ? Number(env.DELAY_MS_OVERRIDE) : entry.delaySeconds * 1000;
    await sleep(ms);
  }
  return json(entry.response, entry.http);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: {
        ...CORS,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "*",
      } });
    }
    let path = url.pathname.replace(/\/+$/, "") || "/";
    if (path === "/health" && request.method === "GET") return json({ ok: true });
    if (path === "/oauth2/token" && request.method === "POST") {
      return json({ access_token: "mock", token_type: "Bearer", expires_in: 1800 });
    }
    if (path.startsWith("/voice/v1/")) path = path.slice("/voice/v1".length);
    const service = path.slice(1);
    if (path.startsWith("/") && Object.prototype.hasOwnProperty.call(SERVICES, service)) {
      if (request.method !== "POST") return json({ hata: "method not allowed" }, 405);
      return handle(service, request, env);
    }
    return json({ hata: "yol bulunamadı" }, 404);
  },
};
