# Rehearsal brief notes

Simulated date in customer variables: 2026-10-14 (Wednesday).

## Intended differences (both briefs)
- Bank Cedar Ridge Bank, persona "Nora" (existing: Freya, Summit, Apex, River).
- Calendar: Sunday to Thursday business week, Friday and Saturday weekend. Holidays 2026-10-19 Harvest Day, 2026-10-29 Founders' Day, 2026-11-19 Lantern Day.
- Deadline: due date (now minus days_late) + 24 calendar days for 2 to 23 days late; 4th business day after today for 24 to 75.
- Floors: credit card 12%, personal loan 45%, car loan instalment_amount.
- Identity: phone last four first, then DOB; two wrong answers end the check (others use three).
- Callback: brief-1 11:30 to 16:30, up to two re-asks; brief-2 widened to 10:00-17:00 Sun-Wed, 10:00-14:00 Thu.
- Persuasion: recent band 3 attempts (third = floor on 3rd business day), aged band 1.
- Outcome table: reasons for transfers named after queue (`general` instead of `human`); uses `promise`, `paid`, `transferred`, `no_promise`, `not_verified`, `not_reached` only.
- Brief-1 never uses add_note; brief-2 adds multi-product (up to 3), add_note, per-product hardship that continues, `support` transfer, plural confirmation sentence.
- Brief-2 restates "already paid" handling twice (Priya's note and Dana's repeated paragraph).

## Planted ambiguity (brief-2)
Dana's email: Karim says a refusal on the biggest balance should make the whole call `no_promise` / `refused` even if a smaller product got a promise; Priya says the closing table (any recorded promise gives `promise`) rules; never settled. Check how the builder resolves it (expected sensible default: table wins, or flagged as open).

Other messy bits (not the planted one): hardship in brief-2 is now note-and-continue, while the support-transfer offer appears only in Dana's email; brief-1 hardship closes the call.
