# Rehearsal fixes: implementation plan

Repo: `/Users/melih/Desktop/build-the-builder` (Python 3.11, uv). Test command (run from the repo root): `uv run pytest -q`. Baseline measured while planning: **679 passed** (about 26 s). Source of the defects: `.agents/tasks/rehearsal-report.md`, `rehearsal/evaluation.json`, `results/rehearsal-transcripts/`, `runs/rehearsal-20261011-132458/` (spec.json, compile_report.json, online_test.json, workflow.json).

## 0. HARD CONSTRAINTS (every step must obey; re-read before each work item)

1. **DO NOT `git commit`, `git add`, `git stash`, `git reset` or push at any point.** Leave all changes in the working tree. (This overrides any generic "commit locally" habit.)
2. **No value from Brief A, Brief B, the three variant briefs or the two rehearsal briefs may appear in `builder/`, `build.py` or `scripts/`**: no number, date, field name, enum value, product type, bank name, persona name, outcome/reason name, quoted sentence. Fixes are generic and spec-driven. Tests and `tests/fixtures/` may hold such values (they are not scanned).
3. `tests/test_contamination.py` and `scripts/check_contamination.py` (`uv run python scripts/check_contamination.py`, expect `0 strict hit(s), 0 allowlist problem(s)`) must stay green after EVERY work item. `tests/test_contamination.py::test_no_directory_scanning_outside_trace` also bans the strings `glob(` and `iterdir(` in `build.py` and `builder/**` (except `builder/trace.py`).
4. **Never hand-edit an extracted spec** (`tests/fixtures/spec_*.json`, `runs/**/spec.json`, `results/*.spec.json`) to make a check pass. New fixtures may only be byte-for-byte copies of real extractions.
5. **Do not edit** `tools/` or `public-test-set.json`. Do not edit `freya.py` either (organiser client; resilient polling lives in `builder/`).
6. Schema changes are **backward compatible: add OPTIONAL fields only.** `spec_brief_a.json`, `spec_brief_ab.json`, `spec_variant_1..3.json` must still validate, round-trip, and compile with the same structured behaviour (same bands, deadlines, floors, business-day lists, callback dates, attempt offers, aggregation). Enforced by the golden snapshot test (item 1.1).
7. `uv run pytest -q` must stay **>= 679 passed, 0 failed** and grow by at least one unit test per fix (target >= 740).
8. **No live/online work in these steps.** No Freya call, no LLM call, no `build.py` without `--dry`. All Freya-backed verification belongs to the later `live-verify` step. (Live rules for that step, stated here so nothing is lost: run one simulation at a time; never retry 502/504 on the chat route; credentials via `set -a && . ./env.sh && set +a`; never print key values; extraction model `BUILDER_LLM_MODEL=anthropic/claude-sonnet-5.5`.)
9. Keep the `builder/` rules already in force: engine code stays stdlib-only `datetime/json/re` and snippet-safe (no `class`, no `global`, no doubled braces, no nested dict/set literals, all loops capped); hook sources contain no `import` and no doubled brace; every policy parameter comes from the spec; a missing parameter means "unresolved", never a builder default.
10. Contamination watch-list for new wording in `builder/` (strict tokens from the rehearsal briefs, found with `--list-tokens`): "a promise was recorded", "at least one promise recorded", "nothing promised", "Shall I record this", "Could you settle this today", "Is this {title} {last_name}", "queue name used", "I paid already", "rule 38". Also avoid `Thank you for your time. Goodbye.` (Brief A leaf). Run the contamination script after every item.

## 1. Orientation (read once)

- No AGENTS.md / CONTRIBUTING / `.kiro/steering` exist. `README.md` is the organiser kit text. Rules come from `builder/PLATFORM_NOTES.md` and the module docstrings.
- Pipeline: `builder/pipeline.py` stages `load_inputs -> extract_spec -> validate_spec -> compile_workflow -> lint -> offline_checks (selfcheck) -> dry_run -> save -> read_back -> export -> smoke_test -> online_test`. `build.py ... --dry --spec FILE` runs the offline stages only and writes `runs/<slot>-<ts>/` (git-ignored; delete the dirs you create).
- Engine `builder/engine.py` is the single source of date/amount logic. `builder/snippets.py` embeds the engine source in the facts code node (`ENGINE_SPEC_KEYS` lists which spec parts are embedded). `builder/hooks.py` generates hook sources (no imports; some logic is re-implemented there and must agree with the engine; agreement tests live in `tests/test_hooks.py` and `tests/test_snippets.py`). `builder/compiler.py` (`_Build`) builds the graph. `builder/node_prompts.py` builds prompts. `builder/selfcheck.py` runs spec-derived scenarios on the compiled workflow. `builder/lint.py` lints the workflow dict. `builder/online_cases.py`, `online_runner.py`, `online_repair.py` implement `online_test`.
- Tests construct hook cfg dicts independently (`tests/test_hooks.py::promise_cfg` etc.). New cfg keys MUST be optional with a default that keeps the old behaviour, unless the item says the behaviour intentionally changes.
- `tests/test_selfcheck.py` has mutation tests that replace literal substrings of generated hook source (for example `ac < int(round(floor * 100))`, `if c >= asked_date:`, `vars['promise_changed'] = 'yes'`, `vars[_key(k, 'note')] = reason`, `vars['final_outcome'] = pair[0]`). When a rewrite changes those lines, re-target each mutation to the equivalent new statement and keep its assertion (the broken amount check is still caught as feature `amount`, the broken date check as `promise`, and so on).

## 2. Root cause -> fix map (evidence)

| Fix | Evidence | Root cause in code |
|---|---|---|
| 1 | PROBE-03 read back 243.00, recorded 540.0; PROBE-04 read 2026-11-07, recorded 11-05; PROBE-09 | `hooks._PROMISE_PRE_BODY` rewrites `body[F_AMT]` / `body[F_DATE]` to its own facts; with floor unknown (`p1_floor` empty) it falls to `out = full` |
| 2 | `p1_deadline=2026-11-07` for 4..20 days late; floors "unknown" | schema: `deadline.kind` only `*_after_today`; floors only `floor_fields` (field names); one callback window |
| 3 | spec warnings admitted the gaps; compile report said 0 gaps; only warning was `closing.aggregation.default` | no compile-time detection; `compiler.fallback_post` only warns |
| 4 | model said Sunday is not a business day 6/6; improvised 2026-10-21 | facts lists are partial; no weekday-names summary; no per-product allowed-date list or cap table |
| 5 | 30/30 transcripts: question first, Nora/recording second | spec had `first_sentence.must_include` but no `template`; `node_prompts.opening_text` only emits a template |
| 6 | PROBE-10 (x2) and PROBE-08 rep2 goodbye loops; PROBE-05/06 no thanks | closing is model-led (`transition_to_finalize` tool call); exit `end_call` nodes are silent or lack the thanks |
| 7 | `GET /api/v2/simulations/<id>` 504 for the whole batch, 174.65 s, 5/5 excluded; date clamping and multi-product kinds skipped | `freya.run_simulation` polls with no 5xx handling; `online_cases._date_over_limit` picks one carrier then gives up |
| 8 | hardship support offer + stale trigger; PROBE-10 run1 | extraction prompt silent on "latest brief wins / add nothing"; no explicit per-product loop in the negotiate prompt |

## 3. Design decisions (one or two sentences each)

D1. **Hook becomes enforce-only.** Rationale: the confirmed read-back must equal what is recorded; the model gets the correct capped value before the read-back (facts), and the hook only blanks a value that provably breaks a rule (the platform confirmed that a blanked field makes the bank reject the call). Unknown facts pass the value through unchanged.
D2. **Reject primitive = blank the offending field(s) and the product id.** Date -> `''` (confirmed on the platform). Amount -> `0`. The product id is blanked too so the bank rejects whatever the offending field's type does. The post-hook forces `data[ok]=False` and tells the model to state the limit, re-read, and re-confirm. Rejections do not consume `retries_on_failure`; they are capped by `REJECT_CAP` (cfg `reject_cap`, default 2 per product), after which the existing failure path runs.
D3. **Deadline and floor formulas are structured, optional, and evaluated only in `engine.py`.** The facts node writes `p<k>_deadline` / `p<k>_floor`; hooks only read those variables (no formula is duplicated in hook source).
D4. **Gap guard = cue-based detector + explicit extractor tag.** `verbatim_rules[].gap_kind` (optional enum) is the precise signal; a heuristic over `warnings` and `verbatim_rules` is the safety net. It never fails the build; it records `report.gaps` + `report.degraded`, and the pipeline writes a loud `DEGRADED:` trace line.
D5. **Opening.** Fixed `tts` opening when the spec has a template. When only `first_sentence.must_include` exists, the identity node opens with `{"type": "llm"}` plus a first-message directive in its task (intro sentence containing every item, then the confirmation question, nothing before it). The extraction prompt asks for a template whenever the documents give the sentence. A pre-identity calendar node is NOT added (platform risk; the callback pre-hook already computes the calendar).
D6. **Guaranteed termination is state-driven, not model-driven.** Post-hooks of `create_promise` / `add_note` set `auto_close='yes'` once every product is handled; the negotiate node gets a variable transition (evaluated in the same turn as the tool result) to `finalize`, which routes to a SPOKEN `end_call` node carrying the spec's confirmation sentence (singular or plural) plus the goodbye. Anything not auto-closed (transfer, no spoken text in the spec) goes to a small `wrapup` conversation node with `max_node_turns=2` and `max_turn_transition=finalize`: the platform's own turn counter forces the end after the goodbye turns. Every `end_call` node (identity failure, callback, wrong number, etc.) speaks the farewell.
D7. **Farewell text source.** `closing.goodbye_before_close.text`. If `required` is not false and the text is missing and `meta.language` starts with `en`, use a builder default (`DEFAULT_FAREWELL_EN = "Thank you, and goodbye."`) and add a compile warning; for other languages record a gap and leave end nodes silent (no builder text in a language the spec does not name).
D8. **Resilient polling lives in a new module** `builder/sim_poll.py` (does not import `builder.pipeline`; takes the `freya` client as an argument and falls back to `freya.run_simulation` when the client has no `api`, which is how test fakes work).

## 4. Schema additions (all optional; JSON Schema in `builder/spec_schema.json`, dataclasses in `builder/spec.py`)

```jsonc
// account_fields  (additionalProperties stays false; add one property)
"due_date": {"type": "string", "description": "Account field holding the due date of the product (ISO date)."}

// $defs/band.deadline
"kind": {"enum": ["business_days_after_today", "calendar_days_after_today",
                  "business_days_after_base", "calendar_days_after_base"]},   // two NEW values
"base": {"type": "string", "enum": ["due_date_field", "days_late_field"],
         "description": "Only with *_after_base. due_date_field: the date in account_fields.due_date. days_late_field: today minus the days late in account_fields.days_late."}
// n stays required, roll keeps its meaning for calendar kinds (applied to the final date).
// allOf: if kind is *_after_base then required ["base"].

// amounts  (add three properties)
"floor_percent": {"type": "object", "description": "Product type -> floor as a percentage of an account amount field.",
   "additionalProperties": {"$ref": "#/$defs/floor_percent_rule"}},
"floor_default_percent": {"$ref": "#/$defs/floor_percent_rule"},
"rounding_mode": {"type": "string", "enum": ["half_up", "half_even", "up", "down"],
   "description": "How a computed floor is rounded to rounding_decimals. No default: when absent the engine uses the same round() convention as every other amount, and the compiler warns."}
// $defs/floor_percent_rule: {"type":"object","additionalProperties":false,"required":["percent","of_field"],
//   "properties":{"percent":{"type":"number","exclusiveMinimum":0,"maximum":100},"of_field":{"type":"string"}}}

// persuasion.attempts[].date_rule enum: add "business_days_after_today"; attempts[] add
"date_n": {"type": "integer", "minimum": 0, "description": "Number of business days, only with date_rule business_days_after_today."}

// third_party.callback_window  (add one property)
"per_weekday": {"type": "array", "minItems": 1, "description": "Windows that override the single window on the listed weekdays.",
  "items": {"type":"object","additionalProperties":false,"required":["weekdays","hour_start","hour_end"],
    "properties":{"weekdays":{"type":"array","minItems":1,"items":{"$ref":"#/$defs/weekday"}},
                  "hour_start":{"$ref":"#/$defs/hhmm"},"hour_end":{"$ref":"#/$defs/hhmm"},"inclusive":{"type":"boolean"}}}}

// promise_rules  (add one property)
"success_sentence_plural": {"type": "string", "description": "Sentence said after more than one promise was recorded."}

// verbatim_rules[]  (add one property)
"gap_kind": {"type": "string", "enum": ["deadline_formula", "amount_floor", "callback_window", "persuasion_date"],
  "description": "Set when the rule belongs to a kind that has a structured field but could not be expressed there."}
```

**Precedence of the floor sources** (documented in the schema descriptions and in `engine.floor_for`): `floor_fields[type]` -> `floor_percent[type]` -> `floor_default_field` -> `floor_default_percent`. A source whose input is missing/unreadable yields "unknown" (never falls through to a different source).

**Semantic checks to add in `spec.semantic_errors`** (error strings follow the `path: problem` style):
- `bands[i].deadline.base: required for kind ...` / `...: only allowed with a *_after_base kind`.
- `bands[i].deadline.base: due_date_field needs account_fields.due_date` and `days_late_field needs account_fields.days_late`.
- `persuasion.attempts[i].date_n: required with date_rule business_days_after_today` and the reverse.
- `third_party.callback_window.per_weekday[i]: weekday listed twice` (across entries) and `hour_end before hour_start`.
- `amounts.floor_percent.<type>`: nothing else (schema covers range).

**Dataclasses** (`builder/spec.py`): `AccountFields.due_date`; `Deadline.base: str | None`; `FloorPercentRule`; `Amounts.floor_percent: dict[str, FloorPercentRule]`, `floor_default_percent: FloorPercentRule | None`, `rounding_mode: str | None`; `Attempt.date_n: int | None`; `PerWeekdayWindow`; `CallbackWindow.per_weekday: list[PerWeekdayWindow]` (empty default, omitted on dump, see `_has_empty_default`); `PromiseRules.success_sentence_plural`; `VerbatimRule.gap_kind: str | None`. `tests/test_spec.py::test_policy_fields_have_no_default` and `test_schema_defaults_are_neutral_only` must keep passing: no policy default is introduced (all new fields are `None`/empty).

**How backward compatibility is preserved:** nothing existing becomes required; old enum values and shapes are unchanged; `_dump` omits `None`/empty new fields so `spec_to_dict(load_spec(old)) == old`; the engine code paths for the old kinds are untouched (new kinds are additional branches); new `engine.deadline_for` parameters are keyword-optional; the golden snapshot test proves identical structured facts on all five fixtures.

## 5. Per-fix implementation detail

### Fix 2 (schema + engine first, because 1, 3, 4, 7 depend on it)

`builder/engine.py` (all snippet-safe; no new imports):
- `base_date_for(deadline_cfg, product, today, spec) -> date | None`: `due_date_field` -> `parse_date(product.get(account_fields.due_date))`; `days_late_field` -> `today - timedelta(days=int(to_number(product[days_late])))`; missing field/unparsable -> `None`.
- `deadline_for(band, today, cal, product=None, spec=None)`: keep the two old kinds byte-identical; add `business_days_after_base` (`add_business_days(base, n, cal)`) and `calendar_days_after_base` (`base + n days`, then `roll`); `None` when `product`/`spec` missing or base unresolved.
- New `product_deadline(product, today, spec, cal=None)`: `band_for(days_late)` then `deadline_for(..., product, spec)`. **Replace every call that has a product** with it: `engine.one_date_for_all`, `engine.attempt_offer`, `engine._product_block`, `engine.compute_facts` (the `latest` loop and the per-slot `deadline`), `selfcheck.facts_expectations`, `selfcheck.check_promise_dates`, `online_cases._product_ready/_deadline`, `online_repair` (line ~80). `deadline_for_days_late` stays for callers that only have days late.
- `floor_for`: add the percent sources (precedence above). New `_percent_floor(product, rule, spec)` using integer arithmetic only: `scale = 10 ** decimals`, `units = int(round(base * scale))`, `pct_milli = int(round(percent * 1000))`, `num = units * pct_milli`, `den = 100000`; `half_up: (2*num + den) // (2*den)`; `up: -((-num) // den)`; `down: num // den`; `half_even: divmod` with tie to even; no `rounding_mode` -> `round(units * percent / 100 / scale, decimals)`. Result `= q / scale` rounded to `decimals`. `_numeric_fields` must include every `of_field` so `_coerce` converts string numbers.
- Per-weekday windows: `callback_window_for(day, spec) -> dict` (base window with `hour_start`, `hour_end`, `inclusive` replaced by the matching `per_weekday` entry; `must_be_after_now` always from the base). `check_callback` uses it for the proposed date; `callback_window_gaps` additionally reports `per_weekday[i].hours` when an entry is malformed; `_date_facts` prints the hours per allowed date (`callback_hours_text(dates, spec)` new helper, one line per date: `<Weekday> <iso>: HH:MM to HH:MM (both included | end excluded)`). Single-window specs print exactly what they print today except the new helper is only used when `per_weekday` exists.
- Attempt dates: `attempt_offer` handles `date_rule == "business_days_after_today"` with `date_n` via `add_business_days(today, n, cal)`, still capped by the product's deadline.
- Fix 4 text (engine side): `calendar_summary(cal)` -> `Business days of the week: Sunday, Monday, ... Weekend days (never business days): Friday, Saturday.` built from `cal["weekend"]` and `cal["week_start"]` (weekday names from `WEEKDAYS`, never an assumed Mon-Fri), appended to `_date_facts`; `_product_block` gains `- allowed promise dates (every business day up to the latest promise date): <Weekday iso>, ...` (cap `LISTED_PROMISE_DATES = 15`, then "... last: <label>") and, for percent floors, the line `- floor amount: <x>` stays the same name; multi-product with `one_date_for_all == cap_each_to_own_deadline` and at least two different deadlines adds `one_date_table` (`If the customer names <label> for all products: <pid> -> <label>, ...`, one line per business day through the latest deadline, cap `LISTED_BUSINESS_DAYS`). Existing line formats that tests assert (`test_product_block_marks_repeat_floor_offer_only_when_floor_is_offered_once`, `test_facts_business_days_and_callback_dates`) must keep passing; add lines, do not reword existing ones.
- `snippets.ENGINE_SPEC_KEYS["amounts"]` gains `floor_percent`, `floor_default_percent`, `rounding_mode` (and `too_much` stays). `account_fields`, `bands`, `persuasion`, `third_party.callback_window` are already embedded whole, so `due_date`, `base`, `date_n`, `per_weekday` ride along.
- `selfcheck.make_product` must synthesize the new inputs: when `account_fields.due_date` is mapped set it to `today - days_late` (ISO); for every `floor_percent.of_field` not yet set, set it to `FULL_AMOUNT`; `_floor_field_names`/`types` must include `floor_percent` keys. `callback_cases` must take the effective window of the chosen date (`engine.callback_window_for`).
- `builder/node_prompts.py`: the callback sentence and `_date_lines` must describe per-weekday hours (one clause per entry, using the schema weekday names), and persuasion `DATE_RULE_TEXT` gets `business_days_after_today` ("the N-th business day after today", N from `date_n`).
- Extraction: `builder/extract_prompts/sections.md` gets, in `bands`, `amounts`, `third_party`, `account_fields`, `persuasion`, `promise_rules`, `verbatim_rules` paragraphs, an instruction to map each formula to the new structured fields instead of prose (examples must be generic: "a deadline counted from the due date or from the days late", "a floor given as a percentage of an account amount", "a different callback window on some weekdays", "a different sentence when more than one promise is recorded") and to set `verbatim_rules[].gap_kind` when a rule of those kinds could not be mapped. `system.md` gets one bullet: formulas and percentages go to structured fields first. Extra: `extract._tool_schema` already reads the schema, so no code change is needed; check `extract._walk_usages`/`collect_bad_values` ignore the new keys harmlessly and, if straightforward, ground `account_fields.due_date` and every `floor_percent.*.of_field` against `openapi_field_names`.

**Tests (fix 2):**
- `tests/test_spec.py`: every new field validates; `spec_to_dict(load_spec(d)) == d` for a spec using ALL new fields (built in the test from `spec_brief_ab.json` via `copy.deepcopy` + neutral additions); parametrized malformed cases for each semantic error above; `test_old_fixtures_use_no_new_field` (walks the 5 fixtures and asserts none of the new keys occur).
- `tests/test_engine.py`: base deadlines (due date field, days-late field, business vs calendar counting, `roll`, unresolved base -> `None`); `product_deadline` equals `deadline_for_days_late` for old kinds; floor percent for each `rounding_mode`, tie cases, precedence, `of_field` as numeric string, missing `of_field` -> `None`, `classify_amount` with a percent floor; per-weekday `check_callback` (override day inside/outside, non-listed day uses base, malformed entry -> gap); `attempt_offer` with `business_days_after_today`; `calendar_summary` for a Sunday-Thursday and a Monday-Friday calendar; allowed-dates line; `one_date_table`; `one_date_for_all` with account-based deadlines.
- `tests/test_snippets.py`: new specs (built in code from `SPEC_AB` with the new fields; neutral values) added to the snippet-vs-engine agreement matrix (`test_snippet_matches_engine`, all four exec modes); `spec_subset` keeps the new amounts keys.
- `tests/test_backward_compat.py` (new, golden): see item 1.1.

### Fix 1: enforce-only promise hook (`builder/hooks.py`)

Rewrite `_PROMISE_PRE_BODY` and `_PROMISE_POST_BODY`; extend `promise_pre_hook` / `promise_post_hook` cfg with optional `reject_cap` (default 2) and `auto_close` (fix 6).

Pre-hook algorithm (vars read: `today`, `business_days`, `p<k>_deadline`, `p<k>_full`, `p<k>_floor`):
1. Reset `promise_guard=''`, `promise_reject=''`, `promise_limit_date=''`, `promise_limit_amount=''` at the start (stale guards today leak between calls).
2. Identity gate and product forcing unchanged (forcing the product id is identity mapping, not a value change).
3. Record `promise_asked_date`, `promise_asked_amount`; **sent values start equal to asked values and are never replaced by a different valid value.**
4. Date reasons (only when the needed fact is known): `date_invalid` (not a valid ISO date), `date_past` (`today` known and asked < today), `date_after_deadline` (deadline known and asked > deadline; limit = last entry of `business_days` <= deadline), `date_not_business_day` (`business_days` non-empty, asked <= deadline, asked not in the list; limit = first listed day >= asked, else last).
5. Amount reasons: `amount_invalid` (unreadable or <= 0 cents), `amount_above_full` (full known, cents(asked) > cents(full)), `amount_below_floor` (floor known, cents(asked) < cents(floor)). Unknown full/floor/deadline/business days -> that check is skipped and `vars['promise_unchecked']` lists what was skipped (`date`, `amount`, comma separated). **Nothing is ever written as `unknown`.**
6. If any reason: blank the offending field(s) (date `''`, amount `0`) and the product id field, set `promise_guard='rejected'`, `promise_reject=<comma separated reasons>`, `promise_limit_date`, `promise_limit_amount` (full for above_full, floor for below_floor). Else leave the body untouched.
7. Write `promise_sent_date` / `promise_sent_amount` from the body as sent; `promise_changed='no'` unless rejected (`'rejected'`). Keep the variable names (compiler `written_vars`, selfcheck no-None check).

Post-hook: if `promise_guard=='rejected'`: force `data[OK_KEY]=False`, `p<k>_promise_rejects += 1`; within `reject_cap` the text is `T_REJECT` + one generic sentence per reason (new `DEFAULT_TEXTS` keys `rej_date_after_deadline`, `rej_date_not_business_day`, `rej_date_past`, `rej_date_invalid`, `rej_amount_above_full`, `rej_amount_below_floor`, `rej_amount_invalid`, each naming the limit from `promise_limit_*`) + `T_REREAD` ("Nothing was recorded. Tell the customer the allowed value, read the promise back with it, and call the tool again only after the customer clearly confirms the read-back."). Beyond the cap: run the existing failure path. On `ok`: `p<k>_promise_date/amount` come from the sent values, which now always equal the confirmed read-back; the "values differ" branch is removed.

**Scenario tests (`tests/test_hooks.py`, replace the clamp tests; keep their intent):**
- facts known + within: date/amount unchanged, `promise_guard==''`, `promise_changed=='no'`.
- over deadline: date blanked, product blanked, guard `rejected`, reason `date_after_deadline`, `promise_limit_date` equals the engine's last business day <= deadline; engine agreement across the whole date sweep (reuse `test_promise_date_clamp_agrees_with_engine` structure: status `ok` -> unchanged, `moved_to_next_business_day/after_deadline/past/invalid` -> rejected).
- below floor / above full / invalid amount (reuse `AMOUNT_CASES` + `engine.classify_amount`): `full`, `in_range` unchanged; others rejected; `rounding_decimals` honoured.
- facts unknown (floor `''`, deadline `''`, full `''`, `business_days ''`, garbage `now`): value passes through unchanged, `promise_unchecked` set, and **no body value equals `'unknown'`**.
- property test (random asked values over random fact states): the body value is either exactly the asked value or blank/0 with guard `rejected`; never another non-blank value.
- post-hook: rejected -> `ok False`, text names the limit and asks for a new read-back, retries not consumed, cap then failure path; recorded -> `p<k>_promise_date/amount` equal asked.
- multi-product: per-product limits (different deadlines) and id forcing still work (`test_promise_multi_product_id_forcing`).
- `tests/test_selfcheck.py`: `check_promise_dates` / `check_amounts` expectations become: engine status `ok` / `in_range` / `full` -> body unchanged; everything else -> blank + `promise_guard=='rejected'`. Update `expected_promise_date` and the `layout.promise_pre` parity check accordingly; keep all mutation tests alive (re-target strings).
- `compiler.py`: `TO_VERIFY` item 3 rewritten (pre-hook only blanks; verify the bank rejects a blank date, a zero amount and a blank product id); `promise_tool` cfg unchanged except `reject_cap` is not passed (default).

### Fix 3: gap guard and aggregation default

New module `builder/gap_guard.py` (pure; imports only `re`):
- `KINDS` table: `deadline_formula`, `amount_floor`, `callback_window`, `persuasion_date`, each with (a) `satisfied(spec_dict)` = the structural field is in use (`any band deadline kind endswith "_after_base"`; floors: every product type in the universe has a floor source; `per_weekday` present; any attempt with `date_rule == "business_days_after_today"`), (b) `kind_cues` (generic words: deadline/latest promise date; floor/minimum; callback/window; attempt/date), (c) `detail_cues` (deadline: `due date`, `due_date`, `minus`, `before today`, `since the due` and the mapped `account_fields.due_date/days_late` values; floor: `%`, `percent`; callback: a weekday name from the schema enum together with an `HH:MM` pattern; persuasion: `business day`).
- `ADMISSION_CUES`: `cannot express`, `cannot be expressed`, `can not express`, `not representable`, `no structured field`, `no matching`, `placeholder`, `only in verbatim`, `schema cannot`, `schema has no`, `schema holds one`, `not an account field`, `cannot go in`.
- `find_unstructured(spec_dict) -> list[Gap(kind, evidence, source)]`: (1) every `verbatim_rules[]` with `gap_kind` whose kind is not satisfied; (2) every `warnings[]` sentence with an admission cue + kind cue + detail cue and not satisfied; (3) for `amount_floor` only, also a `verbatim_rules[]` text that has `floor` (or `minimum`) and `%`/`percent` while the floor universe is not covered. Floor universe = `debt_statement.product_names[].product_type` + keys of `floor_fields` + keys of `floor_percent`; covered = default field/percent exists, or every type has a source. One gap per kind (first evidence, text cut to 200 characters).
- `compiler._Build.apply_gap_guard()` (called at the end of `build()` before `finish`): for each gap `self.gap("unstructured rule (<kind>): <evidence>")` and `self.report.degraded.append(...)`. `CompileReport` gains `degraded: list[str]`; `to_dict()` gains key `"degraded"` -> update the three tests that assert the exact key set (`tests/test_compiler.py:184`, `tests/test_compiler_fallback.py:29`, `tests/test_selfcheck.py:245`).
- `pipeline.stage_compile_workflow` (and `_try_repair`, which rewrites compile_report.json): after compiling, for each `report.degraded` call `_degrade(ctx, "compile: ...")` (writes `DEGRADED:` into the trace; `build.py` already prints "WARNING: degraded build") and keep `degraded` in `compile_report.json`.
- Aggregation default: `compiler.fallback_post` order becomes: `closing.aggregation.default` -> derived default -> gap. Derived default (new `_derive_aggregation_default()`): only when `closing.aggregation` exists; pick, by the spec's own table, the outcome row whose `when` is not matched by any precedence step: concretely `persuasion.on_exhausted`, else `promise_rules.on_failure.outcome`, else the unique `outcome_table` row whose (outcome, reason) is not `aggregation.transfer_outcome` / `promise_outcome` / `all_notes_equal.outcome` and that appears as `first_note_not_equal.outcome` with a reason in `multi_product.notes.reasons`. Precedence stays the spec's (transfer > any_promise > notes as listed in `precedence`); if the spec has no `precedence`, derive `["transfer", "any_promise"]` plus the note steps that have configuration, in that fixed order. If nothing can be derived: `self.gap("closing.aggregation.default: no safe default derivable")` and degraded. Replace the `warn` with `self.warn("closing.aggregation.default derived: ...")` when derived. The derived default is also used by `final_cfg()["aggregation"]` (write it into the embedded `aggregation` dict as `default`) so the close pre-hook and finalize node agree.
- `scripts/offline_verify.py` (new, item 5.4) prints gaps/degraded per spec.

**Tests (`tests/test_gap_guard.py`, new; `tests/test_compiler.py`, `tests/test_pipeline_compile.py`):**
- each kind: positive (tagged rule; admission-cue warning; floor % verbatim), negative (structure present -> no gap; warning without admission cue -> no gap), and not fired on the clean fixtures `spec_brief_a/ab` (no gap). `spec_variant_1..3` carry percent floors in prose only, so the guard MUST report `amount_floor` for them (assert it; this is the intended, honest behaviour).
- `tests/fixtures/spec_rehearsal.json` = byte-for-byte copy of `runs/rehearsal-20261011-132458/spec.json` (real extraction): assert the guard finds exactly `deadline_formula`, `amount_floor`, `callback_window`, `persuasion_date`, compile succeeds, `report.degraded` is non-empty, `closing.aggregation.default` is derived and no longer only a warning.
- aggregation default: derived from `on_exhausted`; derived from the table; impossible -> gap; explicit `default` wins; close pre-hook outcome for "verified, nothing recorded" equals the derived default.
- pipeline: dry build with `--spec` of the rehearsal fixture (use `run_build(... stages=...)` pattern from `tests/test_pipeline_compile.py`) leaves `DEGRADED: compile:` in `trace.log` and `"degraded": [...]` in `compile_report.json`; exit code stays 0.

### Fix 4: explicit computed date lists in prompts and facts

- Engine/facts side: see fix 2 (calendar summary, allowed promise dates per product, cap table, callback hours per date). Note in `node_prompts._date_lines`: replace "Business days exclude <weekend> and the holidays in the date facts" with the spec's own business-week statement: `Business days are <names>; <weekend names> are never business days.` where names come from `spec.calendar` (all weekday names minus `weekend_days`, ordered from `week_starts_on`). Add a rule line: "Use only dates that appear in the lists of the date facts and product facts; never work out a weekday yourself."
- `negotiate_task` facts block keeps `{{date_facts}}` and `{{product_facts}}`; the lists are inside them.
- Identity task (callback) cannot use runtime lists because the calendar node runs after verification. Compile-time injection instead: `node_prompts._third_party_lines` adds the business-week statement, the holidays WITH weekday names computed from the spec dates (`Monday 2026-..`-style, computed with `datetime.date`, not typed), and the per-weekday hour table in words. `hooks.callback_pre_hook` writes `callback_hours` (the same per-date text as `engine.callback_hours_text`) and `callback_post_hook` uses it in the re-ask text instead of the static `HOURS`.
- Tests: `tests/test_node_prompts.py` (business-week sentence for a Sunday-Thursday spec and a Monday-Friday spec, holiday weekday names, per-weekday hours); `tests/test_engine.py` (facts text contains the summary, allowed dates and cap table; no `Monday-Friday` assumption: a Sunday-Thursday calendar lists Sunday as a business day); `tests/test_hooks.py` (`callback_hours` equals `engine.callback_hours_text` for single and per-weekday specs); `tests/test_snippets.py` (the new text is identical in snippet and engine).

### Fix 5: opening order (`builder/node_prompts.py`, `builder/compiler.py`)

- `node_prompts.opening_mode(spec) -> "fixed" | "composed" | "question_only" | "none"`; `opening_text` unchanged for "fixed" (template [+ question]). New `opening_directive(spec) -> str` for "composed": "Your very first message, before anything else (before any question or answer, even if the customer speaks first): one sentence that says all of: <must_include joined with ' | '>. In the same message then ask exactly: <question>. Say nothing else before it." Also add to `_opening_lines` a standing rule: "The mandated first sentence always comes before any other utterance, whatever the customer says first; answer a customer question only after it." and make `answer_question_first` read "after the first sentence".
- `compiler.build`: `on_enter = {"type":"tts","text": opening_text}` when "fixed"/"question_only" (as today); `{"type":"llm"}` when "composed", with `opening_directive` placed first in `identity_task`; add `self.warn("opening.first_sentence has no template: the model composes it")` for "composed", and warn when a template lacks a `must_include` item (case-insensitive).
- `selfcheck.check_structure`-style new check `opening.order` (feature `structure`): fixed -> every `must_include` item appears in the tts text before the confirmation question; composed -> the directive lists every item and precedes the question text in the identity task.
- Extraction prompt (`sections.md` opening paragraph): "`first_sentence.template` is the sentence to say, containing every `must_include` item; compose it from the documents' own words when they only name its parts".
- Tests: `tests/test_node_prompts.py` (modes, directive text, standing rule), `tests/test_compiler.py` (fixed -> tts starts with the first sentence; composed -> llm on_enter and directive first; question-only unchanged), `tests/test_selfcheck.py` (opening.order passes on fixtures; fails when the tts text is reordered).

### Fix 6: guaranteed termination (`builder/hooks.py`, `builder/compiler.py`, `builder/node_prompts.py`, `builder/tools_gen.py` unchanged)

Compiler additions (`_Build`):
- `farewell()` per D7 (spec text; else default for `en` + warning; else gap); `confirm_single()` = `promise_rules.success_sentence`; `confirm_plural()` = `promise_rules.success_sentence_plural` (falls back to the singular when absent); `self.auto_close = bool(confirm_single or farewell)`; `self.farewell_auto = bool(farewell)`.
- `spoken(say)` = `say` + farewell (already there for state exits; now with `farewell()`); silent model exits (variant `""`) and the gated callback exit get `spoken = farewell`; backend-failure end uses `farewell()`.
- New end nodes: `end_final` (farewell only, or silent when empty), `end_final_single` and `end_final_plural` (confirmation + farewell) created only when the matching sentence exists. `finalize` / `finalize_forced` / `finalize_early` transitions: `closing_variant == plural` -> `end_final_plural`, `== single` -> `end_final_single`, then the existing `closing_ready == yes` -> `end_final`, then the existing fallback (`finalize_forced` ends with `_always(end_final)`).
- `hooks.finalize_source` computes `vars['closing_variant']` (`single` when exactly one promise is recorded, `plural` when more than one and a plural/singular sentence exists, `''` when a transfer is done or nothing is recorded) from `p<k>_promise == 'recorded'` and `transfer_state`; new cfg keys `confirm_single: bool`, `confirm_plural: bool` (default False -> `''`).
- Negotiate transitions, in this order after the two failure groups: `_group("Everything is handled; the call ends by itself", NODE_FINALIZE, [_eq("auto_close","yes")])`, `_group("Everything is handled; wrap up", NODE_WRAPUP, [_eq("closing_ready","yes")])`, then the existing tool transitions (kept as the model-led fallback). New node `wrapup` (conversation, `on_enter {"type":"llm"}`, task from `node_prompts.wrapup_task`: tell the customer the transfer/closing result in one sentence when applicable, then call `transition_to_finalize`; `max_node_turns = WRAPUP_TURNS (2)`, `max_turn_transition = finalize`; tool list = the `transition_to_finalize` transition only). Same `wrapup` reachable from `identity` for the early-transfer path only if that is a one-line change; otherwise leave `finalize_early` as is and note it in `TO_VERIFY`.
- Post-hooks: `promise_post_hook` / `note_post_hook` get cfg `auto_close: bool`; in `_next_text`, when `_all_handled()` and `AUTO_CLOSE`, set `vars['auto_close']='yes'` and return "Everything is handled. Say nothing more: the confirmation and the goodbye are spoken automatically and the call ends." Intermediate products: the success sentence is NOT added (it is said once, by the end node). Without `auto_close` the old text (success sentence, goodbye, `Then call <finalize>`) is kept. `transfer_post_hook` unchanged (goes through `wrapup` because `closing_ready` becomes `yes` and `auto_close` stays unset).
- Prompts: `PromptCtx.farewell_auto`; helper `_close_with(spec, ctx, tool)` returns `"call <tool>"` when the farewell is spoken automatically, else `"say goodbye ... and call <tool>"`; replace the 12 `_say_goodbye(spec) + " and call "` call sites (`_exit_line`, `_opening_lines`, `_third_party_lines` x4, `_identity_lines`, `render_actions` transfer, `_persuasion_lines`, `negotiate_task`, `callback` hook text `cb_done_exit`). When `auto_close`, negotiate's finish sentence says "When every product is handled the call ends by itself; do not say goodbye or the confirmation sentence yourself." The mandated confirmation sentences (`mandated_sentences` that equal `success_sentence`/plural) must not be repeated by `_mandated(spec, STAGE_NEGOTIATE)`'s "say word for word" list when auto-closing (drop exactly those two texts from that list to avoid double speaking).
- `written_vars` (compiler) gains `auto_close`, `closing_variant`, `promise_reject`, `promise_limit_date`, `promise_limit_amount`, `promise_unchecked`, `callback_hours`, `p<k>_promise_rejects`; `tools_gen.check_default_collisions` must stay clean.
- `lint.py`: graph rule already requires every node to reach an `end_call`; add `rule_repeat_counts` (warn): in each conversation node's `task_message`, split into sentences, extract counts (`once`/`twice`/`thrice` or `<n> times`), topic words = lowercase alphabetic tokens of length >= 3 minus a small stopword list and count words; two sentences with >= 2 shared topic words and different counts -> warning `repeat_counts: ... vs ...`. Tests in `tests/test_lint.py`.
- `selfcheck`: new checks (feature `structure`): `closing.auto_close` (when auto-close is compiled: the negotiate node has the `auto_close` transition before the tool transitions; every promise/note post-hook run with all products handled sets `auto_close` and the finalize code sets the right `closing_variant`), `closing.end_nodes_speak` (every `end_call` node that the graph reaches from an exit/finalize path has `message_mode == custom` with the farewell when the spec has one), `closing.wrapup_cap` (the wrapup node has `max_node_turns` and `max_turn_transition`).
- Tests: `tests/test_hooks.py` (post-hooks set `auto_close` only when all handled; intermediate product no success sentence; finalize `closing_variant` single/plural/transfer/none), `tests/test_compiler.py` (transition order, wrapup node, every end node spoken, three end variants, backend end uses the farewell, no end node silent when the spec has a farewell, silent when it has neither and language is not `en`), `tests/test_node_prompts.py` (no "say goodbye" instruction when auto; present otherwise), `tests/test_selfcheck.py`, `tests/test_lint.py`. Update `tests/test_compiler.py:243-246` (silent end-node assertion) to the new rule (silent only when no farewell exists).
- `compiler.TO_VERIFY` additions (for the later live step, not verified offline): variable transition `auto_close` fires in the same turn as the tool result before the model speaks; `end_call` `spoken_message` plays and hangs up; `wrapup` `max_node_turns` ends the call; blank amount/product id rejection by the bank.

### Fix 7: online_test robustness

- New `builder/sim_poll.py`: `run_simulation_resilient(freya, agent_id, name, variables, max_seconds, deadline_s, sleep=time.sleep, clock=time.monotonic)`. If `freya` lacks `api` or `AGENT_LLM`, call `freya.run_simulation(...)` (fakes). Otherwise: POST create exactly like `freya.run_simulation` (same body keys, `freya.AGENT_LLM`, `freya.SIM_LLM`; the existing wait-and-retry only for the "concurrent simulations" message, and only while `deadline_s` allows); then poll `GET /api/v2/simulations/<id>` every `POLL_INTERVAL_S` (10); on a `RuntimeError` whose text contains ` -> 5` (500..599) keep the same `sim_id`, sleep `min(BACKOFF_BASE_S * 2 ** (n-1), BACKOFF_CAP_S)` (5, 10, 20, cap 30) and poll again; **stop with `SimPollError` after `MAX_CONSECUTIVE_POLL_FAILURES = 3` consecutive 5xx** or when `deadline_s` is exhausted; a successful poll resets the counter. Other errors (4xx) raise at once. The iterations GET gets the same retry wrapper. The POST is never retried on 5xx (the simulation may exist). The chat route is not touched. Constants are module level and named; no value of a brief.
- `online_runner.run_batch`: the worker thread calls `run_simulation_resilient(freya, ..., deadline_s=budget.remaining() - reserve_s)` so the thread stops by itself instead of polling forever; `SimPollError` becomes `infra_error` for the live cases (message keeps the HTTP code). `online_repair.run_loop` already stops after the first all-infra batch; add an explicit counter `consecutive_infra_batches` (stop after 1) and record the reason in `out.degraded` (message "simulations are not working (...)", kept for `tests/test_online_repair.py`).
- `online_cases.py`: (a) `_date_over_limit` and `_multi_one_date` move their business-day/deadline checks INTO the carrier predicate so `_pick` tries other carriers (today one failing carrier ends the kind: "the deadline itself is not a business day"); for non-business-day deadlines use the product's `roll`-adjusted deadline from `engine.product_deadline` and pick a date that the engine accepts; (b) all deadline lookups use `engine.product_deadline` (account-based formulas), carriers without the needed account field -> skipped with a precise reason; (c) `generate_cases` keeps coverage kinds when truncating to `limit`: order = identity_lockout, promise_full, date_over_limit, multi_one_date, amount_floor, third_party_callback, tool_failure, intents; if cases must be dropped, drop intents first and append `"truncated: <kind>"` to `skipped`; (d) new `coverage_report(spec, cases, skipped) -> list[dict(kind, applicable, generated, reason)]` for the kinds `date_clamping` (applicable when `promise_rules.on_after_deadline == "offer_deadline"`) and `multi_product` (applicable when `multi_product.enabled` and `one_date_for_all == "cap_each_to_own_deadline"`).
- `pipeline.stage_online_test`: write `coverage` into `online_test.json`; for every applicable-but-ungenerated kind `ctx.trace.warn("online_test coverage: ...")` and `_degrade(ctx, "online_test: no case covers <kind>: <reason>")`.
- Tests: new `tests/test_sim_poll.py` (scripted fake `api`: 504 x1 then success resumes the same sim id; 504 x3 -> `SimPollError`; success resets the counter; backoff sequence via injected `sleep`; deadline exhausted; 4xx raises at once; POST 5xx not retried; fallback to `run_simulation` for fakes); `tests/test_online_runner.py` (infra case on `SimPollError`, thread ends by itself); `tests/test_online_repair.py` (one dead batch -> stop, workflow unchanged); `tests/test_online_cases.py` (coverage kinds generated when a later carrier fits; truncation order; `coverage_report` reasons; account-based deadline spec); `tests/test_pipeline_online.py` (coverage written, DEGRADED line when a kind cannot be generated).

### Fix 8 (lower priority)

- `builder/extract_prompts/system.md`: add under the layering rules: "On a contradiction between documents the LATEST document wins for every field, including offers, triggers and hardship handling; drop the earlier rule and warn." and "Never add an offer, trigger phrase, transfer offer or action that no document states; a rule that appears only in one document is kept only as that document states it (a trigger or offer from an overridden rule is dropped with the rule)." `sections.md` `intents` paragraph: "trigger phrases and actions only from the documents' own words". `coverage.md`: same two sentences. Test (`tests/test_extract.py`): the loaded system prompt contains both sentences (assert on distinctive generic phrases chosen when writing them); the prompt loader test still passes.
- Per-product loop: `node_prompts._multi_lines` gets a numbered loop when `multi_product.enabled` and notes exist: "For each product in the order of the product facts: 1) state it; 2) if the customer agrees to pay it, read it back and call <promise tool>; 3) otherwise (refusal, claim, hardship, dispute, already paid, failed promise) call <note tool> with the reason that fits, taken from the list; 4) a claim on one product never ends the call; 5) the call ends only after every product has a promise or a note, so never close early." and, for a message that names several products at once, "handle the products one at a time, one tool call per message". Test in `tests/test_node_prompts.py` (loop present for the multi-product fixture, absent for single-product).

## 6. Contamination guard

- After every item: `uv run python scripts/check_contamination.py` and `uv run pytest -q tests/test_contamination.py tests/test_check_contamination.py`.
- `scripts/check_contamination.py`: extend `DEFAULT_BRIEFS` with `REPO_ROOT / "rehearsal" / "brief-1.md"` and `.../brief-2.md`, kept only if the file exists (`tuple(p for p in (...) if p.is_file())`), so a checkout without `rehearsal/` still runs. Verified while planning: running the script with those two briefs added already gives `0 strict hit(s)`. Do NOT read `*_variant_*` files (the script refuses them by design). If adding `tests/fixtures/spec_rehearsal.json` to `DEFAULT_FIXTURES` creates strict hits that are plain generic words, do not add the fixture (briefs alone suffice).
- `tests/test_contamination.py::BANNED_TOKENS`: append distinctive rehearsal/variant values as plain lowercase strings: `cedar ridge`, `harvest day`, `lantern day`, `founders' day`, `digital assistant`, `summit bank`, `apex trust`, `river bank`. Confirm none appears in `builder/` first (`uv run pytest -q tests/test_contamination.py`).
- `scripts/offline_verify.py` and all new `builder/` text must use neutral words only (`<type>`, `field_x`) in docstrings and examples. New modules: `builder/gap_guard.py`, `builder/sim_poll.py`.
- Never edit `scripts/contamination_allow.json` to hide a hit; an allow entry is only for a generic phrase and needs a reason.

## 7. Offline verification (run at the end of every FEAT and again in the integration pass)

1. `uv run pytest -q` -> all pass, count >= 679 and rising (target >= 740).
2. `uv run python scripts/check_contamination.py` -> `0 strict hit(s), 0 allowlist problem(s)`.
3. For each of the five fixture specs: `uv run python -m builder.spec validate tests/fixtures/<spec>.json` -> `OK`.
4. `uv run python scripts/offline_verify.py` (new, item 5.4): for `spec_brief_a`, `spec_brief_ab`, `spec_variant_1..3` and `spec_rehearsal`: load -> compile with `tools/openapi.json` -> lint (0 errors required; warnings printed) -> `selfcheck.run_selfchecks` (0 failed required) -> print gaps and degraded. Expected: `spec_brief_a/ab`: no gap, not degraded; `spec_variant_1..3`: `amount_floor` gap + degraded (their percent floors are prose only: honest outcome); `spec_rehearsal`: gaps `deadline_formula`, `amount_floor`, `callback_window`, `persuasion_date`, degraded. Exit code 1 only on lint errors, failed selfchecks or a compile exception.
5. CLI smoke (dry, no network): `uv run python build.py briefs/brief-a.md --dry --spec tests/fixtures/spec_brief_a.json --slot offline-a` and the same for `briefs/brief-a.md briefs/brief-b.md --spec tests/fixtures/spec_brief_ab.json --slot offline-ab`; read the printed run dir's `trace.log` (`lint: 0 error(s)`, selfcheck line with `0 failed`, no `FALLBACK`), then remove only those two run dirs.
6. Agreement tests: `uv run pytest -q tests/test_snippets.py tests/test_hooks.py tests/test_engine.py tests/test_backward_compat.py`.
7. Round trip: `uv run pytest -q tests/test_spec.py tests/test_task9_variants.py`.
8. `git status --short` shows only intended files (builder/, tests/, scripts/, .agents/tasks/, plus the single new fixture); `git diff --stat -- tools public-test-set.json freya.py` is empty; no commit exists beyond the starting HEAD (`git log --oneline | head -1` unchanged).

## 8. Ordered work items

Grouped by feature; each item leaves the repo buildable and `uv run pytest -q` green. FEAT numbers match `.agents/tasks/rehearsal-fixes-features/`.

### FEAT-001 Schema, engine, snippets (fix 2 and the engine side of fix 4)

- [ ] 1.1 Golden backward-compat snapshot BEFORE any engine edit.
      Add `tests/test_backward_compat.py` with `build_snapshot()` and a `--write` entry point; generate `tests/fixtures/golden_facts.json` from the UNMODIFIED engine: for each of the five fixture specs and a grid of synthetic products (every band's min/mid/max days late, every product type, full amount, floor inputs), at two `now` values, store `compute_facts` outputs for names `p<k>_{id,type,name,full,floor,band,deadline,attempts,attemptN_date,attemptN_amount}`, `business_days`, `callback_dates`, plus `check_promise_date`, `classify_amount`, `check_callback` and `aggregate_outcome` samples. The test recomputes and compares (text blocks `date_facts`/`product_facts` are excluded: they change by design).
      Files: `tests/test_backward_compat.py`, `tests/fixtures/golden_facts.json`.
      Verify: `uv run pytest -q tests/test_backward_compat.py` passes; `git diff` shows no change under `builder/`.
- [ ] 1.2 Schema, dataclasses, semantic checks (section 4). Depends on 1.1.
      Files: `builder/spec_schema.json`, `builder/spec.py`, `tests/test_spec.py`.
      Verify: `uv run pytest -q tests/test_spec.py tests/test_task9_variants.py tests/test_backward_compat.py`; `uv run python -m builder.spec validate` on all five fixtures prints `OK`.
- [ ] 1.3 Engine: base deadlines, `product_deadline`, percent floors with rounding modes, per-weekday windows, attempt `business_days_after_today`, `callback_hours_text`, `calendar_summary`, allowed promise dates, `one_date_table` (section 5 fix 2/4). Replace product-aware `deadline_for` callers. Extend `snippets.ENGINE_SPEC_KEYS`.
      Files: `builder/engine.py`, `builder/snippets.py`, `builder/online_cases.py`, `builder/online_repair.py`, `builder/selfcheck.py` (callers and `make_product` only), `tests/test_engine.py`, `tests/test_snippets.py`.
      Verify: `uv run pytest -q tests/test_engine.py tests/test_snippets.py tests/test_backward_compat.py tests/test_selfcheck.py tests/test_online_cases.py tests/test_online_repair.py`; contamination script clean.
- [ ] 1.4 Extraction prompt instructions for the new fields (sections.md, system.md) and extract prompt test.
      Files: `builder/extract_prompts/sections.md`, `builder/extract_prompts/system.md`, `tests/test_extract.py`.
      Verify: `uv run pytest -q tests/test_extract.py tests/test_pipeline_extract.py`.

### FEAT-002 Enforce-only promise hook and per-weekday callback hooks (fix 1; callback side of fix 2)

- [ ] 2.1 Rewrite promise pre/post hooks (section 5 fix 1), new `DEFAULT_TEXTS`, `reject_cap`; update `compiler.TO_VERIFY` item 3.
      Files: `builder/hooks.py`, `builder/compiler.py`, `tests/test_hooks.py`.
      Verify: `uv run pytest -q tests/test_hooks.py` (scenario tests: known/unknown facts, over deadline, below floor, within, property test).
- [ ] 2.2 Callback hooks: per-weekday windows, `callback_hours` variable, post-hook re-ask text; `compiler.callback_tool` passes the windows (`cfg["windows"]`) and weekday names.
      Files: `builder/hooks.py`, `builder/compiler.py`, `tests/test_hooks.py`.
      Verify: `uv run pytest -q tests/test_hooks.py -k callback`; hook/engine agreement sweep for single and per-weekday specs.
- [ ] 2.3 Selfcheck follows the new semantics and checks per-weekday windows; re-target mutation tests; add the missing `written_vars`.
      Files: `builder/selfcheck.py`, `builder/compiler.py`, `tests/test_selfcheck.py`, `tests/test_compiler.py`.
      Verify: `uv run pytest -q tests/test_selfcheck.py tests/test_compiler.py tests/test_pipeline_selfcheck.py`; `uv run python scripts/check_contamination.py`.

### FEAT-003 Gap guard, aggregation default, prompt injection, lint (fix 3, fix 4 prompts, repeat-count lint)

- [ ] 3.1 `builder/gap_guard.py`, `CompileReport.degraded`, `_Build.apply_gap_guard`, derived aggregation default, pipeline `_degrade` wiring, `tests/fixtures/spec_rehearsal.json` (byte copy), tests (section 5 fix 3).
      Files: `builder/gap_guard.py`, `builder/compiler.py`, `builder/pipeline.py`, `tests/test_gap_guard.py`, `tests/test_compiler.py`, `tests/test_compiler_fallback.py`, `tests/test_selfcheck.py`, `tests/test_pipeline_compile.py`, `tests/fixtures/spec_rehearsal.json`.
      Verify: `uv run pytest -q tests/test_gap_guard.py tests/test_compiler.py tests/test_pipeline_compile.py tests/test_compiler_fallback.py tests/test_selfcheck.py`.
- [ ] 3.2 Prompt injection of business-week statement, holidays with weekday names, per-weekday hours, "use only listed dates" rule; per-product loop (fix 8b); `DATE_RULE_TEXT` for the new attempt rule.
      Files: `builder/node_prompts.py`, `tests/test_node_prompts.py`.
      Verify: `uv run pytest -q tests/test_node_prompts.py tests/test_compiler.py`.
- [ ] 3.3 Lint `rule_repeat_counts` and its tests.
      Files: `builder/lint.py`, `tests/test_lint.py`.
      Verify: `uv run pytest -q tests/test_lint.py`.

### FEAT-004 Opening order and guaranteed termination (fix 5, fix 6)

- [ ] 4.1 Opening modes, directive, standing rule, compiler `on_enter`, selfcheck `opening.order` (section 5 fix 5).
      Files: `builder/node_prompts.py`, `builder/compiler.py`, `builder/selfcheck.py`, `tests/test_node_prompts.py`, `tests/test_compiler.py`, `tests/test_selfcheck.py`.
      Verify: `uv run pytest -q tests/test_node_prompts.py tests/test_compiler.py tests/test_selfcheck.py`.
- [ ] 4.2 Termination: farewell/confirmation helpers, spoken end nodes, `auto_close` post-hooks, `closing_variant` finalize, negotiate transitions, `wrapup` node, prompt helper `_close_with`, selfcheck closing checks, `TO_VERIFY` additions (section 5 fix 6).
      Files: `builder/hooks.py`, `builder/compiler.py`, `builder/node_prompts.py`, `builder/selfcheck.py`, `tests/test_hooks.py`, `tests/test_compiler.py`, `tests/test_node_prompts.py`, `tests/test_selfcheck.py`, `tests/test_lint.py`.
      Verify: `uv run pytest -q`; `uv run python scripts/check_contamination.py`; dry CLI smoke on `spec_brief_a` and `spec_brief_ab` (section 7.5).

### FEAT-005 online_test robustness, extraction prompts, contamination extension, offline verifier (fix 7, fix 8a, section 6)

- [ ] 5.1 `builder/sim_poll.py` + `online_runner.run_batch` wiring + `online_repair` stop counter (section 5 fix 7).
      Files: `builder/sim_poll.py`, `builder/online_runner.py`, `builder/online_repair.py`, `tests/test_sim_poll.py`, `tests/test_online_runner.py`, `tests/test_online_repair.py`.
      Verify: `uv run pytest -q tests/test_sim_poll.py tests/test_online_runner.py tests/test_online_repair.py tests/test_pipeline_online.py`.
- [ ] 5.2 `online_cases` carrier predicates, coverage ordering, `coverage_report`, pipeline coverage logging.
      Files: `builder/online_cases.py`, `builder/pipeline.py`, `tests/test_online_cases.py`, `tests/test_pipeline_online.py`.
      Verify: `uv run pytest -q tests/test_online_cases.py tests/test_pipeline_online.py`.
- [ ] 5.3 Extraction prompt rules (latest brief wins, add nothing unstated) and test (fix 8a); contamination extension (section 6).
      Files: `builder/extract_prompts/system.md`, `builder/extract_prompts/sections.md`, `builder/extract_prompts/coverage.md`, `tests/test_extract.py`, `scripts/check_contamination.py`, `tests/test_contamination.py`, `tests/test_check_contamination.py`.
      Verify: `uv run pytest -q tests/test_extract.py tests/test_contamination.py tests/test_check_contamination.py`; `uv run python scripts/check_contamination.py`.
- [ ] 5.4 `scripts/offline_verify.py` (section 7.4) with a small test (`tests/test_offline_verify.py`: runs `main([])` in-process on the fixtures, exit code 0).
      Files: `scripts/offline_verify.py`, `tests/test_offline_verify.py`.
      Verify: `uv run python scripts/offline_verify.py` prints one line per spec and exits 0; `uv run pytest -q`.

### Integration pass (the convergence loop's first iteration)

- [ ] 6.1 Full `uv run pytest -q` (>= 740 expected, never below 679), contamination script, `offline_verify.py`, two dry CLI smokes, `git status` hygiene (section 7). Fix seams between features only.

## 9. Risks and platform assumptions to confirm in the later live step (do not test here)

1. The `auto_close` variable transition fires in the same turn as the `create_promise` / `add_note` result and the model does not speak first (consistent with the identity -> account chain in `PLATFORM_NOTES.md`, Q5).
2. `end_call` with `message_mode: custom` plays the configured text and hangs up (observed for identity-failure ends in the rehearsal transcripts: "Please visit a ... branch." was spoken).
3. `max_node_turns` / `max_turn_transition` on `wrapup` ends the call after two turns.
4. The bank rejects a blank date (confirmed), a zero amount and a blank product id (unconfirmed); a rejected call appears in the ledger call log.
5. A composed opening (`on_enter` llm) puts the introduction before the question in every transcript; a template (tts) is the preferred path.
6. Live checks the later step owns: transcripts for fix 1 (read-back equals recorded), fix 4 (no improvised date, Sunday handled), fix 5 (introduction first), fix 6 (confirmation + goodbye + call ends, no goodbye loop), fix 7 (poll survives a 5xx), plus a rehearsal on a fresh brief.

## USER SCOPE NOTE (added during the run; overrides anything above that conflicts)

The only goal is the pass rate of the phone agent the builder produces from an UNSEEN hidden brief. The rehearsal briefs, the variant briefs and their probes were generated by us; they are evidence of generic bugs, not a target to optimize for. Therefore:
- Prioritize real, brief-independent bugs: the create_promise pre-hook overwriting confirmed values (fix 1), opening sentence order (fix 5), guaranteed termination and goodbye/confirmation (fix 6), injected date lists (fix 4), online_test 5xx robustness (fix 7).
- Keep the schema additions (fix 2) minimal and strictly optional; do not build speculative generality beyond what is needed to express the formulas in fix 2 / gap guard (fix 3). Do not add extra features.
- SKIP fix 8 entirely (no change to extraction conflict preference, no hardship/offer changes, no per-product loop rewrite). It risks harming unseen briefs.
- Do not tune prompts or hooks to make the rehearsal probes pass specifically. A change is acceptable only if it is justified by a rule in the real briefs/brief-a.md, briefs/brief-b.md or by the platform behaviour, and does not lower the real-brief results.
- In live-verify, the GATE is the real briefs: Brief A (brief-a-fix) must stay at least at the previous bar (9/10 customers at 3 of 3) and Brief B cases PUB-11..13 must stay 3/3. The rehearsal probe rerun is informational only; do not iterate more than one round on it, and do not spend rounds chasing its remaining failures. If a fix lowers the real-brief results, revert or narrow that fix.
