The PolicySpec you produced failed validation. Fix exactly the problems listed and return the complete corrected PolicySpec.

Rules:
- Keep every value that was not reported as wrong. Do not drop rules to make validation pass; if a rule no longer fits a structured field, move its exact words into `verbatim_rules`.
- A path such as `a.b[2].c` points into your previous output. `required` means add the field using the documents; if the documents do not state it, fix the structure in the way that omits invented facts and add a warning.
- `unexpected property` means remove or rename that property to one the schema has. If the content matters, keep its words in `verbatim_rules`.
- `not in closing.outcome_table` means either the reference uses a different name than the table (make them agree using the documents' exact names) or the table is missing a row the documents give (add it).
- Never use null. Do not invent values.
