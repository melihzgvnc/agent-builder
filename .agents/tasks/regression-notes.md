# Part 3 regression: Brief A rebuilt with Sonnet (slot brief-a-sonnet)

Command: `BUILDER_LLM_MODEL=anthropic/claude-sonnet-5.5 uv run python build.py briefs/brief-a.md --slot brief-a-sonnet` (full pipeline, no --dry, no --spec). Slots 'brief-a', 'base', 'brief-ab' were not touched. No git commit.
Saved agent_id 23ce2887-e473-49a6-bec1-c7e4f6f4ad59, workflow_id 2c8ea1a5-5167-4597-a02d-993547f61238.
Files: results/brief-a-sonnet.spec.json, results/brief-a-sonnet.trace.log, results/brief-a-sonnet.online_test.json, results/brief-a-sonnet-stability.json. runs/ folder deleted after copying.

## Build (from results/brief-a-sonnet.trace.log)
- extract_spec 84.44 s (model call 70.83 s + coverage call 13.49 s), 11 warnings, verbatim_rules=35, no fallback.
- compile 34 nodes, 0 gaps; lint 0; offline checks 116, 0 failed; dry_run, save, read_back (0 diffs), export ok; smoke 2.15 s.
- online_test: 8/8 pass, 0 repairs, 208.15 s.
- Total wall time `duration: 296.65 s` (4.9 min), 19.8% of the 1500 s (30 min) budget.

## Stability: tools/run_cases.py 23ce2887... --brief A --reps 3
- Run 1: 10/10 (147 s). Run 2: 9/10 (201 s). Run 3: 9/10 (219 s). Script total 567 s.
- **Stability number: 9/10 customers pass in all 3 runs (28/30 individual calls).** Only PUB-10 (tool failure) fails: 1/3.
- PUB-10 failure analysis: the ledger check passes every time (exactly one promise after the retry; tool order verify, verify, get_account, create_promise, create_promise, close_call). The failing check is "agent does not say 'recorded' before a successful call". In the one failure I could read in full (run 3), the judge flagged the word "recorded" in message 0, the opening sentence "...this call is recorded", which brief rule 1 mandates. Run 2's reason was cut off in the console log and I did not read the stored text for it. Run 1 passed the same check with the same opening, so this looks like judge variance on a brief-mandated sentence, not an agent defect. I did not change anything. I did not run the Haiku-built agent against PUB-10 in this step, so I do not claim Haiku behaves differently.
- Console warnings: "phone agent was slow" in runs 1 and 3 (median slowest turn 11 s and 10 s); run 2 median 2 s. Not retried.
- For context, earlier Brief A entries in results/log.jsonl (3 reps): 6/10, 9/10, 9/10, 10/10, 7/10. Their agents/extraction models are not recorded in the log, so no Haiku-vs-Sonnet claim is made from them.

## Sonnet vs Haiku spec (scripts/diff_spec.py expects `<expected_spec.json> <extracted_spec.json>`; run as `results/brief-a.spec.json results/brief-a-sonnet.spec.json`)
Note: brief-a.spec.json is the baseline spec; it differs from results/extract-haiku-a.json, which I did not diff. I treated brief-a.spec.json as the Haiku extraction as the task states.
Result: 12/14 key facts identical. Calendar, bands, deadlines, identity order and max wrong, amounts fields, persuasion sequence, outcome table, multi_product all match.

Differences that affect behavior:
1. Intents. Sonnet has two intents the Haiku spec lacks, both from the brief: `change_or_cancel_promise` (rule 36: "You can change it in the mobile app.") and `unknown_question` (rule 44: "I do not have that information."). With Haiku, these two cases rely on the prompt alone.
2. Goodbye. Sonnet puts an explicit `say "Thank you, goodbye."` before every close in every intent (rule 47). The Haiku intents close without it (the generic closing text may still carry it; not checked).
3. Opening. Sonnet adds `opening.first_sentence.template` = "I am a digital assistant calling from Freya Bank, and this call is recorded." and lists it as a mandated sentence (Haiku: only must_include keywords, no template). This is the sentence the PUB-10 judge tripped on once.
4. Intent precedence/triggers. Sonnet sets precedence on all intents (robot/scam 5, hardship 10, restructuring 20, human 30, already_paid/dispute 40, change promise 50, refuse 60, unknown 90) and keeps `applies_before_verification` on robot, scam, human, unknown, refuse-before-verification. Haiku has hardship 10, refuse 50, human 60 and none on others, so ordering when two intents match differs. Sonnet's trigger lists are narrower (no "I am sick", "I lost my job", "lower my payments", "is this a machine", "I have already paid"); Haiku's lists are broader and likely match more paraphrases.
5. Intent names differ (ask_if_robot_or_ai vs robot_or_ai_question, dispute vs debt_not_mine, etc.). Names are cosmetic.
6. Sonnet adds `closing.aggregation.default` = no_promise/refused (fallback when no promise and no transfer), and `promise_rules.readback.template` ("Read back the product, the amount and the date.").
7. Wording: below-floor "This amount cannot be recorded." (Haiku "That amount..."), promise failure "I could not record it." (Haiku "I could not record your promise."), identity prompt "Please tell me your date of birth." (Haiku "What is your date of birth?"), identity decline explain "The questions protect your account.". Haiku's day_month_swapped hint says the retry does not count as a wrong answer in the instruction text; Sonnet's says "and retry" (counts_as_wrong is false in both).
8. Persuasion flat-refusal definition: Sonnet's text additionally names a below-floor correction refusal as a flat refusal; Haiku lists non-answer, floor refusal and deadline refusal. Minor.
Other: Sonnet 11 warnings vs 19 in brief-a.spec.json; verbatim_rules 35 (Sonnet) vs 23 (Haiku spec). Tools block: Haiku has `purpose` fields, Sonnet has `notes`; cosmetic.

## Wall time vs the 1500 s budget (all values read from trace.log or online_test JSONs)
| run | total (`duration:`) | extraction | online_test | source |
|---|---|---|---|---|
| Brief A Sonnet (full pipeline) | 296.65 s (4.9 min) | 84.44 s | 208.15 s | results/brief-a-sonnet.trace.log |
| variant-2 (full pipeline, live extraction) | 355.13 s (5.9 min) | 133.72 s | 209.56 s | results/variant-2.trace.log |
| variant-3 final run (--spec, no extraction) | 339.55 s | 0 (spec file) | 334.80 s | results/variant-3.trace.log |
| variant-1 final run (--spec) | 160.22 s | 0 (spec file) | 152.11 s | results/variant-1.trace.log |

- Longest single pipeline run with live extraction that I saw in a trace: variant-2, 355.13 s = 23.7% of the budget. The variant-3 --spec run was 339.55 s without its ~73 s extraction (extraction 72.94 s per .agents/tasks/variant-runs-notes.md, not re-read from a trace since the trace was overwritten by the --spec rerun), so a full variant-3 build with the same online_test would be roughly 410 s (estimate, not measured). Still well under 1500 s.
- Brief A plus the 3-rep stability script: 296.65 s + 567 s of simulations = 863.65 s of work, run sequentially (wall time of the stability script itself was not timestamped, so this is a sum of reported numbers, not a clock reading).

## Checks
- `uv run pytest -q`: 679 passed (>= 616). tests/test_contamination.py: 2 passed. `scripts/check_contamination.py`: 0 strict hits, 0 allowlist problems.
- runs/ is empty (the brief-a-sonnet run folder was deleted after copying artifacts to results/).
