// Mock bank Worker. Data driven: every tool, field name, enum and role comes
// from ./config.json (written by gen_mock.py from the service contract) and
// every customer from ./data.json. Nothing here is specific to one brief.
//
// Storage: in-memory Map cache with write-through to an optional KV binding
// named LEDGER_KV. Without the binding ledgers live in memory only (see the
// `storage` field of GET /v1/health).
import CONFIG from "./config.json" with { type: "json" };
import DATA from "./data.json" with { type: "json" };

const JSON_HEADERS = { "content-type": "application/json" };
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
const MAX_WRONG = 3;

function reply(status, body) {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });
}

function validDate(s) {
  if (typeof s !== "string" || !DATE_RE.test(s)) return false;
  const d = new Date(s + "T00:00:00Z");
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === s;
}

function swapDayMonth(s) {
  if (typeof s !== "string" || !DATE_RE.test(s)) return null;
  const [y, m, d] = s.split("-");
  return Number(d) <= 12 ? `${y}-${d}-${m}` : null;
}

function newState(customerId, caseId) {
  return {
    customer_id: customerId,
    case_id: caseId,
    verified: [],
    wrong: 0,
    closed: false,
    fault_counts: {},
    ledger: {
      ok: true, outcome: null, reason: null,
      promises: [], notes: [], callbacks: [], transfers: [], failures: [], calls: [],
    },
  };
}

export function createHandler(config, data, defaultEnv) {
  const cache = new Map();
  const byCase = new Map();
  for (const c of data.customers || []) byCase.set(String(c.id), c);
  const toolByPath = new Map();
  for (const [name, t] of Object.entries(config.tools || {})) toolByPath.set(t.path, name);
  const roleOf = {};
  for (const [role, name] of Object.entries(config.roles || {})) {
    if (typeof name === "string") roleOf[name] = role;
  }
  const gated = new Set(config.gated_tools || []);
  const cidKey = (config.fields && config.fields.customer_id) || "customer_id";

  function kv(env) { return (env && env.LEDGER_KV) || null; }

  async function load(env, cid) {
    if (cache.has(cid)) return cache.get(cid);
    const store = kv(env);
    if (store) {
      const raw = await store.get("s:" + cid);
      if (raw) { const st = JSON.parse(raw); cache.set(cid, st); return st; }
    }
    // A customer id that matches a known case id (or its variables) starts a
    // fresh state on first use, so the mock also works without a session call.
    for (const c of byCase.values()) {
      const vid = c.variables && c.variables[cidKey];
      if (String(c.id) === cid || (vid && String(vid) === cid)) {
        const st = newState(cid, String(c.id));
        cache.set(cid, st);
        return st;
      }
    }
    return null;
  }

  async function save(env, st) {
    cache.set(st.customer_id, st);
    const store = kv(env);
    if (store) await store.put("s:" + st.customer_id, JSON.stringify(st));
  }

  function validate(tool, args) {
    const props = tool.props || {};
    for (const key of tool.required || []) {
      if (args[key] === undefined || args[key] === null || args[key] === "") return `missing field: ${key}`;
    }
    for (const [key, spec] of Object.entries(props)) {
      const v = args[key];
      if (v === undefined || v === null) continue;
      if (spec.type === "string" && typeof v !== "string") return `${key} must be a string`;
      if ((spec.type === "number" || spec.type === "integer") && (typeof v !== "number" || !Number.isFinite(v))) return `${key} must be a number`;
      if (spec.type === "integer" && !Number.isInteger(v)) return `${key} must be an integer`;
      if (spec.type === "boolean" && typeof v !== "boolean") return `${key} must be a boolean`;
      if (spec.enum && !spec.enum.includes(v)) return `${key} must be one of: ${spec.enum.join(", ")}`;
      if (spec.format === "date" && !validDate(v)) return `${key} must be a date as YYYY-MM-DD`;
      if (spec.format === "time" && !TIME_RE.test(v)) return `${key} must be a time as HH:MM`;
    }
    return null;
  }

  function strip(args) {
    const out = { ...args };
    delete out[cidKey];
    return out;
  }

  function behave(role, name, args, st, customer) {
    const f = config.fields || {};
    const led = st.ledger;
    switch (role) {
      case "verify": {
        const key = args[f.verify_key];
        const answer = String(args[f.verify_answer]).trim();
        const idn = customer.identity || {};
        const need = Object.keys(idn).length || config.identity_required_count || 1;
        if (st.wrong >= MAX_WRONG) return { ok: false, error: "too many wrong answers" };
        if (!(key in idn)) return { ok: false, error: "unknown field" };
        const expected = String(idn[key]);
        if (DATE_RE.test(expected) && !validDate(answer)) {
          return { ok: false, error: "unreadable answer" };
        }
        if (answer === expected) {
          if (!st.verified.includes(key)) st.verified.push(key);
          return { ok: true, verified: true, wrong_answers_left: MAX_WRONG - st.wrong, all_verified: st.verified.length >= need };
        }
        st.wrong += 1;
        const res = { ok: true, verified: false, wrong_answers_left: MAX_WRONG - st.wrong, all_verified: false };
        const hint = config.verify_hint;
        if (hint && swapDayMonth(answer) === expected) res[hint.key] = hint.value;
        return res;
      }
      case "account":
        return { ok: true, products: customer.products || [] };
      case "promise":
        led.promises.push(strip(args));
        return { ok: true, promise_id: `pr_${led.promises.length}` };
      case "note":
        led.notes.push(strip(args));
        return { ok: true };
      case "callback":
        led.callbacks.push(strip(args));
        return { ok: true };
      case "transfer":
        led.transfers.push(strip(args));
        return { ok: true };
      case "close":
        led.outcome = args[f.close_outcome] ?? null;
        led.reason = args[f.close_reason] ?? null;
        st.closed = true;
        return { ok: true };
      default:
        return { ok: true };
    }
  }

  async function callTool(env, name, tool, args) {
    const cid = String(args[cidKey] ?? "");
    const st = cid ? await load(env, cid) : null;
    if (!st) return reply(config.error_status ?? 200, { ok: false, error: "unknown customer" });
    const customer = byCase.get(st.case_id) || {};
    if (st.closed) return reply(config.error_status ?? 200, { ok: false, error: "call already closed" });
    const role = roleOf[name] || "other";
    const led = st.ledger;

    // Fault injection comes from the customer's faults list.
    for (const fault of customer.faults || []) {
      if (fault.tool !== name) continue;
      const seen = st.fault_counts[name] || 0;
      if (seen < Number(fault.fail_first_n || 0)) {
        st.fault_counts[name] = seen + 1;
        const result = { ok: false, error: "simulated failure" };
        led.calls.push({ tool: name, args, result });
        led.failures.push({ tool: name, args });
        await save(env, st);
        return reply(config.fault_status ?? 500, result);
      }
    }

    let result;
    if (gated.has(name) && st.verified.length < (Object.keys(customer.identity || {}).length || 1)) {
      result = { ok: false, error: "identity not verified" };
    } else {
      result = behave(role, name, args, st, customer);
    }
    led.calls.push({ tool: name, args, result });
    await save(env, st);
    return reply(200, result);
  }

  return async function fetch(request, env = defaultEnv) {
    const url = new URL(request.url);
    const method = request.method.toUpperCase();
    const path = url.pathname.replace(/\/+$/, "") || "/";
    const prefix = config.base_path || "/v1";
    const teamKey = env && env.TEAM_KEY;
    const teamHeader = (config.team_header || "x-team-key").toLowerCase();
    const authorised = () => !teamKey || request.headers.get(teamHeader) === teamKey;

    if (method === "GET" && path === prefix + "/health") {
      return reply(200, { ok: true, storage: kv(env) ? "kv" : "memory", tools: Object.keys(config.tools || {}), customers: byCase.size });
    }

    if (method === "POST" && path === prefix + "/practice/sessions") {
      if (!authorised()) return reply(401, { ok: false, error: "unauthorised" });
      let body;
      try { body = await request.json(); } catch { return reply(400, { ok: false, error: "invalid JSON" }); }
      const caseId = String((body && (body.case ?? body.case_id ?? body.id)) ?? "");
      const customer = byCase.get(caseId);
      if (!customer) return reply(404, { ok: false, error: "unknown case" });
      const cid = "c_" + crypto.randomUUID().replace(/-/g, "").slice(0, 12);
      await save(env, newState(cid, caseId));
      return reply(200, { ok: true, variables: { ...(customer.variables || {}), [cidKey]: cid } });
    }

    const ledgerPrefix = prefix + "/practice/ledger/";
    if (method === "GET" && path.startsWith(ledgerPrefix)) {
      if (!authorised()) return reply(401, { ok: false, error: "unauthorised" });
      const st = await load(env, decodeURIComponent(path.slice(ledgerPrefix.length)));
      if (!st) return reply(404, { ok: false, error: "unknown customer" });
      return reply(200, st.ledger);
    }

    const relative = path.startsWith(prefix + "/") ? path.slice(prefix.length) : null;
    const name = relative ? toolByPath.get(relative) : undefined;
    if (name) {
      if (method !== "POST") return reply(405, { ok: false, error: "method not allowed" });
      let args;
      try { args = await request.json(); } catch { return reply(400, { ok: false, error: "invalid JSON" }); }
      if (!args || typeof args !== "object" || Array.isArray(args)) return reply(400, { ok: false, error: "body must be a JSON object" });
      const problem = validate(config.tools[name], args);
      if (problem) return reply(config.error_status ?? 200, { ok: false, error: problem });
      return callTool(env, name, config.tools[name], args);
    }
    return reply(404, { ok: false, error: "not found" });
  };
}

const handler = createHandler(CONFIG, DATA);

export default {
  async fetch(request, env, ctx) {
    return handler(request, env);
  },
};
