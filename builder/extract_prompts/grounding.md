A PolicySpec uses values for tool fields (identity field names, queue names, note reasons, close outcomes) that are not in the allowed values of the tool API. For each item, choose the allowed value that means the same thing, or null when no allowed value has the same meaning.

You receive a JSON list of items: {"id", "kind", "value", "allowed"}. Reply with a JSON object {"mappings": [{"id": <id>, "replacement": "<allowed value or null>"}]}. Use only values from that item's `allowed` list. Do not force a match: use null when the meanings differ.
