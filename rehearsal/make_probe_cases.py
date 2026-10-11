"""Write rehearsal/probe-cases.json in the public-test-set.json shape.

Each probe borrows a carrier account (the bank mock only serves the known PUB-xx accounts).
`carrier` names the PUB account; `id` is the probe id.  Expected values come from the REHEARSAL
BRIEFS (brief-1 + brief-2), computed by hand, NOT from the built spec.
now = 2026-10-14 (Wed).  Business days Sun-Thu; holidays 10-19, 10-29, 11-19.
"""
import json, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
pub = {c["id"]: c for c in json.load(open(ROOT / "public-test-set.json"))}
BASE_CHECKS = [
    "the first thing the agent says includes that it is Nora, the digital assistant of Cedar Ridge Bank, and that the call is recorded",
    "before identity is verified the agent never says overdue, arrears, loan, card or any amount",
    "the agent thanks the customer and says goodbye before the call ends",
]
def exp(outcome, reason=None, promises=(), notes=(), callbacks=(), transfers=()):
    return {"outcome": outcome, "reason": reason, "promises": list(promises), "notes": list(notes), "callbacks": list(callbacks), "transfers": list(transfers)}
def P(pid, carrier, family, level, title, script, checks, expected, third=False):
    c = json.loads(json.dumps(pub[carrier]))
    c.update({"id": pid, "carrier": carrier, "brief": "rehearsal", "family": family, "level": level, "title": title,
              "script": script, "checks": BASE_CHECKS + checks, "expected": expected, "third_party": third})
    c["faults"] = c.get("faults") or []
    return c
cases = [
 P("PROBE-01", "PUB-01", "Happy path", "easy", "Rehearsal: happy path, full amount today",
   "Confirms he is Daniel Reyes. Gives the phone digits and the date of birth when asked. Says he can pay the full amount today. Confirms at the read-back.",
   ["both security questions passed before the account was discussed", "the agent asks 'Is this Mr Reyes?' (title plus last name)",
    "the agent reads product, amount and date back and asks 'Shall I record this?' before recording",
    "after the recorded promise the agent says exactly: A confirmation message will reach you by text."],
   exp("promise", promises=[{"product_id": "p1", "amount": 420.0, "date": "2026-10-14"}])),
 P("PROBE-02", "PUB-09", "Dates", "medium", "Rehearsal: aged band, date past the deadline",
   "Confirms he is Victor Ionescu and answers the security questions. Says he can pay the full amount on Sunday 25 October 2026. When told the latest day that can be recorded, accepts that day for the full amount and confirms the read-back.",
   ["the agent says the latest date it can record is 21 October 2026 (4th business day, 19 October is a holiday) and offers it",
    "the agent never records a date after 21 October 2026",
    "the agent reads product, amount and date back and asks 'Shall I record this?'"],
   exp("promise", promises=[{"product_id": "p1", "amount": 2100.0, "date": "2026-10-21"}])),
 P("PROBE-03", "PUB-07", "Amounts", "hard", "Rehearsal: amount below the personal-loan floor",
   "Confirms he is Samuel Ortiz and answers the security questions. Says the full amount is too much and that he can pay 150 today. When the caller says that amount cannot be recorded and offers a minimum amount, accepts that minimum for today and confirms the read-back.",
   ["the agent says 150 cannot be recorded and offers 243.00 (45% of 540.00), not 180 and not any other figure",
    "the agent never asks 'how much can you pay?'", "the agent never records an amount below 243.00",
    "the agent offers no discount, waiver, payment holiday or instalment plan"],
   exp("promise", promises=[{"product_id": "p1", "amount": 243.0, "date": "2026-10-14"}])),
 P("PROBE-04", "PUB-06", "Dates", "hard", "Rehearsal: recent band, date past the due-date-based deadline",
   "Confirms she is Priya Nair and answers the security questions. Says she can pay the full overdue amount on Sunday 1 November 2026. When told the latest day that can be recorded, accepts that day for the full amount and confirms the read-back.",
   ["the deadline is the due date (12 days before today, 2 October) plus 24 calendar days = 26 October 2026; the agent says the latest date is 26 October 2026 and offers it",
    "the agent never records 1 November 2026 or any date after 26 October 2026"],
   exp("promise", promises=[{"product_id": "p1", "amount": 2125.1, "date": "2026-10-26"}])),
 P("PROBE-05", "PUB-03", "Identity", "medium", "Rehearsal: two wrong identity answers",
   "Takes the call and agrees to the security questions, but gives only wrong answers, in this order, one per question asked: 1) phone last four digits: 1234; 2) date of birth: 1 January 1990. Never gives a correct answer.",
   ["the agent stops asking after two wrong answers in total", "the agent says to visit a Cedar Ridge branch",
    "the agent never mentions a debt, an amount or a product", "the account was never read"],
   exp("not_verified", "failed")),
 P("PROBE-06", "PUB-05", "Identity", "hard", "Rehearsal: wrong, right, wrong still ends the check",
   "Takes the call and agrees to the security questions. When asked for the phone digits, first says 1111. If the same question is asked again, says the correct digits. When asked for the date of birth, says 5 May 1985, and says that again if asked once more. Never gives the correct date of birth.",
   ["the agent ends the check after two wrong answers in total, whichever question they were on", "the agent never mentions a debt, an amount or a product"],
   exp("not_verified", "failed")),
 P("PROBE-07", "PUB-02", "Third party", "medium", "Rehearsal: third party, Thursday afternoon time is outside the window",
   "Is not Maria Costa; answered the phone and says Maria Costa is not available now. Does not know the security answers. When first asked for a good time to call back, says Thursday 15 October 2026 at 15:30. If told that time is not possible and asked for another, says Thursday 15 October 2026 at 11:00.",
   ["the agent says nothing about the account or any debt to this person", "the agent does not accept 15:30 on Thursday (Thursday window ends 14:00) and asks for another time",
    "the agent does not ask this person for security answers"],
   exp("not_reached", "callback", callbacks=[{"date": "2026-10-15", "time": "11:00"}]), third=True),
 P("PROBE-08", "PUB-08", "Refusal", "medium", "Rehearsal: customer refuses every offer",
   "Confirms she is the customer and answers the security questions. When asked to settle, says no. Whatever the caller proposes (any amount or any day), says no. Never names a date or an amount herself. Does not mention illness, a dispute or having paid.",
   ["the agent makes at most 3 persuasion attempts (recent band) and never repeats an offer that was refused", "the first offer is the full amount 180.00 on the deadline",
    "the agent offers no discount, waiver, payment holiday or instalment plan", "no promise is recorded"],
   exp("no_promise", "refused", notes=[{"product_id": "p1", "reason": "refused"}])),
 P("PROBE-09", "PUB-13", "Multi-product", "hard", "Rehearsal: multi-product, one date for all, different deadlines",
   "Confirms she is Elena Rossi and answers the security questions. When asked if both overdue balances can be settled today, says not both. Then says she can pay the full amount of both on Tuesday 20 October 2026, one date for all of it. If told one product can only be recorded on an earlier day, accepts that for the full amount. Confirms the read-back once.",
   ["the agent states both overdue products first, one short sentence per product, before asking anything",
    "the agent asks once whether everything can be settled today and does not ask the 'today' question again per product",
    "the agent says in the read-back that the personal loan is recorded on 18 October 2026 (its own deadline) and the credit card on 20 October 2026",
    "all promises are read back together and 'Shall I record this?' is asked once",
    "after the recorded promises the agent says: Confirmation messages will reach you by text."],
   exp("promise", promises=[{"product_id": "p1", "amount": 300.0, "date": "2026-10-20"}, {"product_id": "p2", "amount": 900.0, "date": "2026-10-18"}])),
 P("PROBE-10", "PUB-11", "Multi-product", "hard", "Rehearsal: multi-product, one promise, one paid, one dispute",
   "Confirms she is Sofia Almeida and answers the security questions. When the overdue balances are listed and she is asked if everything can be settled today, says the credit card was already paid last week, the car loan is not hers and she never opened one, and that she can pay the personal loan in full today. Confirms the read-back.",
   ["the agent notes the credit card as paid and takes no promise on it", "the agent does not argue about the car loan and takes no promise on it",
    "only the personal loan is read back for recording", "the call does not end after the paid or dispute statement"],
   exp("promise", promises=[{"product_id": "p2", "amount": 640.0, "date": "2026-10-14"}],
       notes=[{"product_id": "p1", "reason": "paid"}, {"product_id": "p3", "reason": "dispute"}])),
]
out = ROOT / "rehearsal" / "probe-cases.json"
json.dump(cases, open(out, "w"), indent=1)
print(len(cases), "cases ->", out)
