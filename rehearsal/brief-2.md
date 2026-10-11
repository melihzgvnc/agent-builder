# Cedar Ridge Bank: multi-product extension (raw material from the project folder)

Fictional bank, synthetic customers. This is everything product sent over for the multi-product version. It sits on top of brief-1 (Cedar Ridge, Nora). Everything in brief-1 still applies unless something below says otherwise.

---

From: Priya (Product)
Subject: Re: multi-product calls, v1
Date: Mon 5 Oct

Hi all,

Short version: a customer can now be late on up to three products at once (credit card, personal loan, car loan), and `get_account` hands them back as a list. Nora should first say all of them, one short sentence per product, and only then ask for anything. We ask once whether they can settle everything today, and if not we go product by product in the order the list came back in. Please do not ask the "today" question again per product, it annoys people.

Deadline and band are still worked out per product from that product's own days late, so two products can have different deadlines. Persuasion attempts are counted per product, and each product starts again from attempt 1. Floors are per product too, using the same percentages as before.

---

Meeting notes, Wed 7 Oct (Karim, Priya, Dana, Tomas)

- Karim wants every product to end up with something on file. If the customer promises a product, we use `create_promise`. If not, we use `add_note` with the product id and one of: refused, hardship, dispute, paid, service_error. Dana: this also applies when nothing at all is promised.
- Tomas asked about one date for everything. Agreed: if the customer says "Thursday for all of it" and Thursday is past one product's deadline, that product is recorded on its own deadline and Nora says so in the read-back. The others keep Thursday.
- Read all promises back in one go, ask once, then one `create_promise` per product. Nobody wants three confirmations.
- A dispute or an "I already paid" on one product does not end the call. Note it, move on.
- Callback window gets longer for this version: 10:00 to 17:00 on Sunday to Wednesday and 10:00 to 14:00 on Thursday, still today or next business day only.
- Dana raised hardship. Old behaviour was close the call. New: sympathy sentence, note that product with hardship, carry on with the remaining products.
- Transfers: no notes at all before or after a transfer. Promises already recorded before the transfer stay. Agreed but unrecorded promises are dropped.

Action: Tomas to turn this into the spec. Priya to double check the closing table.

---

From: Dana (Collections ops)
Subject: closing, and a thing I forgot
Date: Thu 8 Oct

Closing table for the multi-product case. This is what I think we agreed:

| Situation | Outcome | Reason |
|---|---|---|
| at least one promise recorded | `promise` | none |
| nothing promised, every product noted paid | `paid` | none |
| nothing promised, anything else | `no_promise` | reason of the first product (list order) that is not paid |
| transfer happens | `transferred` | queue name used |
| one `create_promise` fails twice | keep the others, note `service_error` on that one | n/a |

Forgot to say: when the customer asks for a human, ask once whether Nora can help, then transfer to `general` if they insist. Support queue is new for us. If someone is clearly struggling (illness, job loss) and says yes to speaking to a support colleague, transfer to `support` and close as `transferred` / `support`.

Also, to be clear on the "already paid" thing, a customer saying a product was settled recently should be handled by noting it as paid and not taking a promise on it. Same thing as above, just wanted it in the spec.

One more: Karim's view is that a refusal on the biggest balance means the whole call counts as `no_promise` / `refused` even if a small product got a promise, because the big one is what the bank cares about. Priya disagrees, says the table above is the rule. We did not settle it before the call ended.

---

Example call (Tomas wrote this to show tone), customer is Mr Okafor, now = Wed 2026-10-14 11:00:

> Nora: Hello, this is Nora, Cedar Ridge Bank's digital assistant. This call is recorded. Is this Mr Okafor?
> Customer: Yes.
> Nora: (phone digits, then date of birth, both pass) Thank you. You have two overdue balances: your credit card ending 1234, 180.00 late by 9 days, and your car loan, 640.00 late by 40 days. Could you settle both today?
> Customer: Not both. I can do the card on Sunday.
> Nora: Sunday 18 October works. Full 180.00? ... The car loan then: could you pay 640.00 by Tuesday?
> Customer: I told you, no.
> Nora: (one persuasion attempt for the car loan on its deadline, refused) I will note that. To confirm: card, 180.00 on 2026-10-18. Shall I record this?

---

Reference tables Tomas pulled together:

| Product | Floor |
|---|---|
| credit card | 12% of past due |
| personal loan | 45% of past due |
| car loan | `instalment_amount` |

| Days late | Deadline | Attempts |
|---|---|---|
| 2 to 23 | due date + 24 calendar days | 3 |
| 24 to 75 | 4th business day after today | 1 |

Calendar is the same as brief-1: Sunday to Thursday are business days, Friday and Saturday are not, holidays are 2026-10-19, 2026-10-29 and 2026-11-19.

Last note from Tomas: the sentence after recording changes to the plural when more than one promise was recorded: "Confirmation messages will reach you by text." Everything else in the identity step is unchanged (two questions, phone first, two wrong answers end it).
