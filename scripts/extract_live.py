"""Run the real extractor (network). Not part of pytest.

usage: uv run python scripts/extract_live.py --openapi tools/openapi.json --out results/spec.json brief1.md [brief2.md ...]
Model/key come from the environment (see builder/llm.py); the key is never printed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from builder.extract import extract_spec  # noqa: E402
from builder.llm import LLMClient, LLMConfigError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("briefs", nargs="+")
    ap.add_argument("--openapi", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--budget", type=float, default=480.0)
    args = ap.parse_args()
    try:
        llm = LLMClient()
    except LLMConfigError as e:
        print(f"LLM not configured: {e}", file=sys.stderr)
        return 2
    print(f"config: {llm.config}")
    report: dict = {}
    t0 = time.monotonic()
    spec, warnings = extract_spec(args.briefs, args.openapi, llm, budget_s=args.budget, report=report)
    secs = time.monotonic() - t0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    print(f"fallback={report.get('fallback')} mode={report.get('mode')} repairs={report.get('repairs', 0)}")
    print(f"calls={llm.calls} input_tokens={llm.total_input_tokens} output_tokens={llm.total_output_tokens} seconds={secs:.1f}")
    print(f"verbatim_rules={len(spec.get('verbatim_rules', []))} missing_after={len(report.get('missing_after') or [])}")
    print(f"warnings ({len(warnings)}):")
    for w in warnings:
        print(f"  - {w[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
