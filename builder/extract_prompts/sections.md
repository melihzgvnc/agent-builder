Guidance for each section of the PolicySpec. Fill a section only with facts the documents state.

meta: the institution name, how the agent names itself, language, currency, and one `sources` entry per document in order (path, order starting at 1, role such as base or amendment, one-line summary).

call_variables: names of the variables every call starts with (customer id, current date and time, name parts, title). Use the names the documents give. Extra variables go in `extra`.

account_fields: the field names the account tool returns for each product (product id, product type, days late, the key of the products list). Each value is the exact key as it appears in the account tool response in the tool API summary (look the field up there by meaning), never a concept name you made up.

opening: what the agent says first, words forbidden before identity is verified, whether the rules apply to everyone, the question that confirms who is on the line, which answers count and which do not, how many times to ask, what to do with an unclear answer, and the fixed answers to questions such as whether the caller is automated or whether the call is genuine.

third_party: what to do when someone other than the customer answers: what may be said, whether to ask for a better time, the callback window (days rule, hours, inclusive or not, must be after now), how many re-asks, outcomes for each branch (callback scheduled, no valid time, wrong number, customer deceased with the required sentence).

identity: the security questions in the order asked (`field` is the API field name), the prompt for each, the answer format and any conversion the agent must do, the total number of wrong answers allowed, whether wrong answers may be re-asked, hints and whether they count as wrong, what to say and which outcome applies on failure, and what to do when the customer declines to answer.

debt_statement: how the debt is stated after verification: intro, product type names with templates (`product_type` uses the exact API value), which fields are stated, one sentence per product or not.

calendar: weekend days, the list of holidays as ISO dates, first day of the week, how "next" and "this" weekday are read, how a vague period such as "next week" is handled.

bands: one entry per lateness range. Each has an inclusive min and max of days late, a deadline (kind of count such as calendar days or business days, the number n, and how to roll when it lands on a non-business day) and the number of persuasion attempts. Bands must not overlap. Compute nothing that the documents do not state.

amounts: which account field holds the full amount, which field is the floor per product type (the keys of `floor_fields` are the exact allowed values of the product type field in the account response of the tool API summary, not display names), the default floor field, whether the agent asks the customer for an amount, forbidden questions, what to do when the customer offers more than allowed or less than the floor, rounding.

promise_rules: the earliest and latest promise date rules, business-day requirements, what to say when the date is not allowed or is after the deadline, how the promise is read back and confirmed, one promise per product, changes and cancellations, retries when the tool fails, the sentence that confirms success, the failure branch and forbidden offers.

persuasion: the ordered attempts (index, which date rule, which amount rule, which bands), what counts as a flat refusal, whether a customer proposal is ever a refusal, not repeating a refused offer, and the outcome when attempts are exhausted.

intents: things the customer may say at any time (for example disputing the debt, saying it is already settled, financial difficulty, asking for a person, asking to change the terms, asking to stop calls, and similar). Each has a name, trigger phrases, ordered actions, precedence (lower number is checked first, default 100), whether it applies before and after verification, and how many times it must occur before the action. Action types are limited to the schema enum; use `close` with an outcome and reason from the outcome table.

closing: `outcome_table` has one row per outcome and reason combination with a plain-language `when` and source references. Also: whether the close tool is always last, exactly one close, whether a goodbye is required and its text, and the `aggregation` rules, also for a single product (precedence of outcomes, transfer outcome, promise outcome, all-notes-equal rule, first-note rule, default). Without `aggregation` the built agent reports its fallback outcome even when a promise or transfer was recorded.

multi_product: whether the customer can be late on several products, the maximum, order of handling, whether to state all first, whether to ask about paying everything today first, per-product attempts, one date for all and how a capped deadline is announced, combined read-back, notes for products without a promise and their reasons, what happens on transfer and on a failed promise.

tools: per tool (use only tool names from the TOOL API block), optional purpose, `field_map`, `allowed_values` and notes. Always fill `field_map` for every tool the documents use. Its keys are these semantic names per tool: verify_identity: customer, question, answer; get_account: customer; create_promise: customer, product, amount, date; add_note: customer, product, reason, text; schedule_callback: customer, date, time; transfer_to_agent: customer, queue; close_call: customer, outcome, reason. Its values must be the exact request field names of that tool in the TOOL API block. `allowed_values` maps an API request field name to the list of allowed values, copied from the API enum.

Keep the output compact: do not repeat the same rule text in several fields beyond what the schema needs, and keep `notes` short.

mandated_sentences: sentences required word for word, with when they are said.

verbatim_rules: every other rule, word for word. Allowed stages: opening, third_party, identity, debt, negotiate, closing, global. Use `global` for rules that apply to the whole call. Include prohibitions, tone rules, anything about what the agent must or must not say or do that no structured field holds.

coverage: optional map from each source rule reference to the spec paths that implement it. Only list paths that exist in your output.

warnings: missing facts, ambiguities, overridden rules, values you could not match to the API.
