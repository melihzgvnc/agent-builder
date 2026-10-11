from __future__ import annotations

import sys

from tests.conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import offline_verify  # noqa: E402


def test_offline_verify_pattern(capsys) -> None:
    assert offline_verify.main([]) == 0
    lines = {l.split(":")[0]: l for l in capsys.readouterr().out.splitlines() if l.startswith("spec_")}
    assert "gaps=- degraded=no" in lines["spec_brief_a"] and "gaps=- degraded=no" in lines["spec_brief_ab"]
    for n in (1, 2, 3):
        assert "gaps=amount_floor degraded=yes" in lines[f"spec_variant_{n}"]
    for kind in ("deadline_formula", "amount_floor", "callback_window", "persuasion_date"):
        assert kind in lines["spec_rehearsal"]
