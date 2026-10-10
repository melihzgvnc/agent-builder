# Real extractions compile to a real workflow: field_map/enum derivation and compile-feedback repair

The compiler now derives missing `field_map` entries from `tools/openapi.json` using explicit entries, alias keys, name/type/enum cues and single-gap elimination. It raises a precise `CompileError` listing candidates when a required field is ambiguous. Extraction passes the semantic names and exact API field names in the prompt, strips non-API values, and runs up to two budget-gated compile-feedback repairs before the pipeline fallback. Tests cover derivation, the repair loop and real compilation of all variants.
Watch for: exact-name derivations are not logged (possible, minor); the variant test proves "no CompileError" rather than asserting `fallback_workflow` directly (possible, minor); a `selfcheck.py` behavior change is bundled (confirmed, non-blocking).

**Verdict**: APPROVED

## High-level view

Derivation is conservative. Explicit entries always win. A cue is accepted only when exactly one unclaimed property fits. Elimination fires only when exactly one required semantic and one non-customer property remain. Anything else raises `cannot derive field for 'x'; candidates [...]` for verify/promise/close, and optional roles keep the drop-with-warning path. Cues are role/semantic constants and generic English, with no brief values.

Enums already come from the openapi in tool schemas, since spec `allowed_values` is read nowhere else. The compiler therefore only logs "taken from the openapi enum" and the "spec allows values the enum lacks" warning. A bad extraction cannot reach tool schemas, which satisfies the intent without the planned `self.allowed` dict.

The repair loop sits after grounding, only runs when compile fails, is capped at `COMPILE_REPAIRS = 2`, and is gated on `asker.remaining() > 30`. It merges patches by top-level section, discards patches that fail `validate_spec`, and never raises. Fallback remains the last resort.

<details>
<summary>Issues (3)</summary>

1. **Identity derivations unlogged** (possible) — in `_derive_fields`, a derived mapping where semantic name equals API name and the sem is not in `fmap` is skipped in the warning loop. The task says to log every derived mapping; consider logging them or documenting the exception.
2. **No direct `fallback_workflow` assertion** (possible) — `test_variant_compiles_to_real_workflow` calls `compile_workflow` directly, so success implies no fallback. An assertion through `run_build`/`compile_report.json` would be more literal and guard pipeline-level fallback causes (for example a selfcheck repair path).
3. **Bundled selfcheck change** (confirmed, non-blocking) — `selfcheck.py` (untracked, so absent from the diff) skips a comparison when finalize decided no outcome. The change is generic and justified in the report for variant 2, but it is separate from the compile fix and weakens a check slightly.

</details>

<details><summary>Details</summary>

## Field derivation boundaries
`_cue_match` returns a field only when a tier yields exactly one hit, and returns None on multiple hits, so it never picks arbitrarily. Enum preference breaks ties only for question/queue/outcome/reason. Elimination excludes the customer field. Ambiguity tests assert "cannot derive" plus candidate names. Derived non-identity mappings are logged as "derived field_map 'q' -> 'field' ... (why)", and explicit-but-wrong values warn and then derive. Identity matches such as `answer -> answer` are silent (Issue 1).

## Repair loop
The loop's invalid-patch handling, LLMError handling, low-budget skip, zero calls for a compilable spec, and the cap of 2 are each covered by FakeLLM tests. The truncation retry now adds a compact hint instead of repeating an identical request. Not tested: a repair that succeeds on the second attempt after an invalid first patch.

## Evidence
The report records `uv run pytest -q` at 599 passed and `scripts/check_contamination.py` exiting 0 (0 strict hits). I did not re-run them. A diff read of builder/ found only role/semantic constants and generic prose; the rewording of the `intents` and `opening` prompt examples removes brief-flavored phrases rather than adding them.

</details>

<details><summary>File map</summary>

- builder/compiler.py: `SEMANTIC_CUES`, `_derive_fields`, `_cue_match`, enum warnings, `report.context` (the fallback builders in the diff come from earlier phases)
- builder/extract.py: `_fill_tool_fields`, `_try_compile`, `_compile_repair_loop`, truncation hint, report fields
- builder/extract_prompts/sections.md, compile_repair.md: semantic names, API names, compactness
- builder/selfcheck.py: skip placeholder comparison when no outcome
- tests/test_compile_fix.py, tests/test_task9_variants.py: new coverage

Full diff: `git diff` in the repo root.

</details>
