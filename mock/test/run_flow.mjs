// Run a scripted flow against a generated worker, in one process so state persists.
//   node run_flow.mjs DIR < steps.json
// steps.json: [{"method","path","body","headers"}...]. The string "$customer_id"
// anywhere in a path or body is replaced by the customer id of the latest
// session response. Prints {steps:[{status,body}], ledger}.
import { load } from "./harness.mjs";

const chunks = [];
for await (const c of process.stdin) chunks.push(c);
const steps = JSON.parse(Buffer.concat(chunks).toString("utf8") || "[]");
const call = await load(process.argv[2]);

let customerId = null;
const sub = (s) => (customerId ? s.replaceAll("$customer_id", customerId) : s);
const results = [];
for (const step of steps) {
  const body = step.body === undefined ? undefined : JSON.parse(sub(JSON.stringify(step.body)));
  const res = await call(step.method || "POST", sub(step.path), body, step.headers || {});
  if (res.body && res.body.variables && res.body.variables.customer_id) customerId = res.body.variables.customer_id;
  results.push(res);
}
const ledger = customerId ? (await call("GET", `/v1/practice/ledger/${customerId}`)).body : null;
console.log(JSON.stringify({ steps: results, ledger }));
