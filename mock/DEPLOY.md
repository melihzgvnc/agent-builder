# Deploying the mock bank

Nothing here has been deployed by the builder. Every command below is run by you.

The mock is generated from the service contract and the test customers, so it works for any contract with POST tools. It validates the contract and records calls. It does not enforce the brief's policy; the ledger scorer judges that.

## Prerequisites

- Node 18+ and a Cloudflare account (Free plan is enough).
- Wrangler: `npx wrangler --version` (or `npm i -g wrangler`).

## 1. Generate

```bash
python mock/gen_mock.py --contract PATH/TO/openapi.json --customers PATH/TO/customers.json \
  --out mock/dist --name my-bank-mock
```

This writes `worker.js`, `config.json`, `data.json`, `wrangler.toml` into `mock/dist`. Read the printed tool/role summary. A `WARNING (loud)` means the contract was not recognised and the mock falls back to generic recording; check `config.json` by hand.

`mock/dist` is not git-ignored: commit it if you want the deployed version on record.

## 2. Log in and (optionally) create durable storage

```bash
cd mock/dist
npx wrangler login
npx wrangler kv namespace create LEDGER_KV
```

Paste the returned id into `wrangler.toml` and uncomment the `[[kv_namespaces]]` block. Skipping this keeps ledgers in memory only: a recycled isolate loses them. `GET /v1/health` reports `"storage": "kv"` or `"memory"`.

| Option | Consistency | Setup | Notes |
| --- | --- | --- | --- |
| Memory only | per isolate, lost on recycle | none | fine for a quick smoke test |
| KV (default here) | eventual (up to ~60 s across locations) | one command | one customer is normally served from one location |
| Durable Object | strong | migration + class | not used here; more moving parts |

## 3. Deploy

```bash
npx wrangler deploy
```

The output ends with `https://<name>.<subdomain>.workers.dev`. That is your base URL.

## 4. Smoke test

```bash
URL=https://<name>.<subdomain>.workers.dev
curl -s $URL/v1/health
curl -s -X POST $URL/v1/practice/sessions -H 'content-type: application/json' -d '{"case":"<a customer id from data.json>"}'
curl -s -X POST $URL/v1/<verify tool> -H 'content-type: application/json' \
  -d '{"customer_id":"<id from the session>", ...}'
curl -s $URL/v1/practice/ledger/<id from the session>
```

## 5. Point the builder at it

```bash
python build.py ... --server-url $URL/v1      # or: export BUILDER_SERVER_URL=$URL/v1
```

## Optional team key

```bash
npx wrangler secret put TEAM_KEY
```

When set, the session and ledger endpoints require the `x-team-key` header with that value (401 otherwise). Tool endpoints stay open, as in the contract: the customer id is the secret.

## Watching and teardown

```bash
npx wrangler tail                      # live request log
npx wrangler delete                    # remove the Worker
npx wrangler kv namespace delete --namespace-id <id>
```

## Limits (Free plan)

100k requests/day and 1000 KV writes/day. Each tool call writes once, so a final run of a few hundred calls is well inside the limit.

## Endpoints served

- `POST /v1/<tool>` for every contract operation
- `POST /v1/practice/sessions` `{ "case": "<customer id>" }` returns `{ ok, variables }` with a fresh customer id
- `GET /v1/practice/ledger/:customer_id`
- `GET /v1/health`
