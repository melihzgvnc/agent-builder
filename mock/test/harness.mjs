// Offline harness: loads a generated worker (DIR/worker.js) and calls its
// fetch handler with an in-memory fake KV. No network, no Cloudflare.
//   node harness.mjs DIR METHOD PATH [JSON_BODY]   -> prints {status, body}
import path from "node:path";
import { pathToFileURL } from "node:url";

export function fakeKV() {
  const m = new Map();
  return {
    async get(k) { return m.has(k) ? m.get(k) : null; },
    async put(k, v) { m.set(k, v); },
  };
}

export async function load(dir, env = {}) {
  const mod = await import(pathToFileURL(path.resolve(dir, "worker.js")).href);
  const fullEnv = { LEDGER_KV: fakeKV(), ...env };
  return async function call(method, p, body, headers = {}) {
    const init = { method, headers: { "content-type": "application/json", ...headers } };
    if (body !== undefined && body !== null) init.body = typeof body === "string" ? body : JSON.stringify(body);
    const res = await mod.default.fetch(new Request("https://mock.test" + p, init), fullEnv, {});
    const text = await res.text();
    let parsed;
    try { parsed = JSON.parse(text); } catch { parsed = text; }
    return { status: res.status, body: parsed };
  };
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [dir, method, p, body] = process.argv.slice(2);
  const call = await load(dir);
  console.log(JSON.stringify(await call(method, p, body)));
}
