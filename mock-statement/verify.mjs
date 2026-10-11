// Usage: node verify.mjs            (offline, imports handler)
//        BASE_URL=https://... node verify.mjs   (live)
import worker from "./worker.js";
import { readFileSync } from "node:fs";
const data = JSON.parse(readFileSync(new URL("./data.json", import.meta.url)));
const BASE = process.env.BASE_URL;
const ENV = BASE ? {} : { DELAY_MS_OVERRIDE: "50" };
let pass = 0, fail = 0;
const ck = (ok, name, extra = "") => { if (ok) pass++; else { fail++; console.log("FAIL", name, extra); } };

async function call(path, data_) {
  const init = { method: "POST", headers: { "content-type": "application/json", "user-agent": "Mozilla/5.0" },
    body: JSON.stringify({ data: data_, call: { id: "x" } }) };
  const t = Date.now();
  const r = BASE ? await fetch(BASE + path, init) : await worker.fetch(new Request("https://m.test" + path, init), ENV);
  const text = await r.text();
  let body; try { body = JSON.parse(text); } catch { body = text; }
  return { status: r.status, body, ms: Date.now() - t, ct: r.headers.get("content-type") };
}
const strip = (b) => { const c = JSON.parse(JSON.stringify(b)); if (c?.result) { delete c.result.guid; if (c.result.details) delete c.result.details.guid; } return c; };
const eq = (a, b) => JSON.stringify(sortK(a)) === JSON.stringify(sortK(b));
const sortK = (o) => Array.isArray(o) ? o.map(sortK) : o && typeof o === "object" ? Object.fromEntries(Object.keys(o).sort().map((k) => [k, sortK(o[k])])) : o;

async function expectEntry(name, path, req, e) {
  const prefixes = ["/voice/v1/", "/"];
  for (const p of prefixes) {
    const r = await call(p + path, req);
    ck(r.status === e.http && eq(strip(r.body), strip(e.response)), `${name} [${p}]`, `got ${r.status}`);
    if (p === "/voice/v1/") ck(/application\/json; charset=utf-8/.test(r.ct || ""), name + " content-type");
    if (e.delaySeconds && !BASE) ck(r.ms >= 40, name + " delayed");
    if (e.delaySeconds && BASE) ck(r.ms >= e.delaySeconds * 1000 - 200, name + " delayed live", String(r.ms));
    if (e.delaySeconds) break; // only once for the slow one
  }
}

for (const c of data.customers) {
  for (const [svc, ent] of Object.entries(c.services)) {
    const path = svc;
    if (svc === "cards.list" || svc === "contact.email.get" || svc === "statement.pdf.send") {
      await expectEntry(`${c.customerNo} ${svc}`, path, { customerNo: c.customerNo }, ent);
      // numeric customerNo variant
      const r = await call("/voice/v1/" + path, { customerNo: Number(c.customerNo) });
      ck(r.status === ent.http && eq(strip(r.body), strip(ent.response)), `${c.customerNo} ${svc} numeric`);
    } else if (svc === "cards.balance" || svc === "statement.week.get") {
      for (const [card, e] of Object.entries(ent)) await expectEntry(`${c.customerNo} ${svc} ${card}`, path, { cardNumber: card }, e);
    } else {
      for (const [card, weeks] of Object.entries(ent)) for (const [w, e] of Object.entries(weeks)) {
        await expectEntry(`${c.customerNo} ${svc} ${card} w${w}`, path, { cardNumber: card, newStatementWeek: w }, e);
        const r = await call("/voice/v1/" + path, { cardNumber: ` ${card} `, newStatementWeek: Number(w) });
        ck(r.status === e.http && eq(strip(r.body), strip(e.response)), `${c.customerNo} ${svc} ${card} w${w} numeric`);
      }
    }
  }
}

// fallbacks
const shape = (r, status, code, type, name) => {
  const x = r.body?.result;
  ck(r.status === status && !("data" in (r.body || {})) && x && x.returnCode === code && x.code === code &&
    x.messageText === x.message && x.type === type && /^[0-9a-f]{32}$/.test(x.guid) && x.details?.guid === x.guid, name, JSON.stringify(r.body));
};
const nf = [["cards.list", { customerNo: "00000000" }, "FB_IVR_CHANNEL_102"], ["cards.balance", { cardNumber: "999999******9999" }, "FB_IVR_CHANNEL_120"],
  ["statement.week.get", { cardNumber: "999999******9999" }, "FB_IVR_CHANNEL_121"], ["statement.pdf.send", { customerNo: "00000000" }, "FB_IVR_CHANNEL_107"]];
for (const [s, d, code] of nf) shape(await call("/voice/v1/" + s, d), 404, code, "WARNING", s + " 404");
for (const [s, d] of [["statement.week.decide", { cardNumber: "999999******9999", newStatementWeek: "2" }], ["statement.week.change", { cardNumber: "540000******1234", newStatementWeek: "1" }], ["contact.email.get", { customerNo: "00000000" }]])
  shape(await call("/voice/v1/" + s, d), 400, "INVALID_ARGUMENTS_ERROR", "ERROR", s + " noentry 400");
const miss = [["cards.list", {}, "customer number"], ["cards.balance", { cardNumber: "  " }, "card number"], ["statement.week.decide", { cardNumber: "540000******1234" }, "new statement week"],
  ["statement.week.change", { newStatementWeek: "2" }, "card number"], ["contact.email.get", { customerNo: "" }, "customer number"], ["statement.pdf.send", {}, "customer number"], ["statement.week.get", {}, "card number"]];
for (const [s, d, f] of miss) { const r = await call("/voice/v1/" + s, d); shape(r, 400, "INVALID_ARGUMENTS_ERROR", "ERROR", s + " missing 400"); ck(r.body?.result?.message?.includes(f), s + " names field", r.body?.result?.message); }
// template placeholders
for (const [s, d] of [["cards.list", { customerNo: "{{customerNumber}}" }], ["cards.balance", { cardNumber: "%7B%7BcardNumber%7D%7D" }], ["statement.week.get", { cardNumber: "{{c}}" }],
  ["statement.week.decide", { cardNumber: "{{c}}", newStatementWeek: "{{w}}" }], ["statement.week.change", { cardNumber: "{{c}}", newStatementWeek: "{{w}}" }], ["contact.email.get", { customerNo: "{{x}}" }], ["statement.pdf.send", { customerNo: "{{x}}" }]]) {
  const r = await call("/voice/v1/" + s, d); ck(r.status === 200 && r.body?.result?.returnCode === "1", s + " template", r.status);
}
// routing
const g = async (p, m = "GET", body) => { const init = { method: m, headers: { "user-agent": "Mozilla/5.0" }, body }; const r = BASE ? await fetch(BASE + p, init) : await worker.fetch(new Request("https://m.test" + p, init), ENV); return { status: r.status, body: await r.json().catch(() => null) }; };
let r = await g("/health"); ck(r.status === 200 && r.body?.ok === true, "health");
r = await g("/oauth2/token", "POST", "{}"); ck(r.status === 200 && r.body?.access_token === "mock" && r.body?.expires_in === 1800, "token");
r = await g("/nope", "POST", "{}"); ck(r.status === 404 && r.body?.hata === "yol bulunamadı", "404 path");
r = await g("/voice/v1/cards.list", "GET"); ck(r.status === 405, "405");

console.log(`${BASE ? "LIVE" : "OFFLINE"}: PASS ${pass} FAIL ${fail}`);
process.exit(fail ? 1 : 0);
