# statement-mock

Cloudflare Worker mock of the Freya Bank statement-debt services. It serves the exact `http` status and `response` body for every customer, card and week in `data.json` (a copy of `test-customers.json`). Unknown customers or cards get the contract's 404 (or a 400 `INVALID_ARGUMENTS_ERROR` where the service has no 404). Empty required fields get a 400 naming the field. Template placeholders (`{{...}}`, `%7B%7B...`) get the first valid record. The one entry with `delaySeconds` waits that long first.

URL: https://statement-mock.melihzgvnc.workers.dev

Endpoints (POST, with or without `/voice/v1`): `cards.list`, `cards.balance`, `statement.week.get`, `statement.week.decide`, `statement.week.change`, `contact.email.get`, `statement.pdf.send`. Also `GET /health` and `POST /oauth2/token`.

## Verification

`verify.mjs` checks every entry in the data (status and body, guids ignored), plus 404/400 fallbacks, placeholders and routing.
- Offline (imports the handler): 2370 checks passed, 0 failed.
- Live (`BASE_URL=https://statement-mock.melihzgvnc.workers.dev node verify.mjs`): 2370 checks passed, 0 failed, including the delayed entry.

## Redeploy

```
set -a && . ../env.sh && set +a
npx wrangler deploy < /dev/null
```

The data is synthetic. The endpoint is unauthenticated (as the contract allows), public and stateless.
