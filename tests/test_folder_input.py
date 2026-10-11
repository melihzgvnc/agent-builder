from __future__ import annotations
import shutil
from pathlib import Path
from builder.folder_input import classify_json, discover, extra_text
from tests.conftest import REPO_ROOT
def _folder(tmp_path: Path) -> Path:
    d = tmp_path / "in"
    d.mkdir()
    shutil.copy(REPO_ROOT / "briefs" / "brief-a.md", d / "zzz-notes.md")
    shutil.copy(REPO_ROOT / "briefs" / "brief-b.md", d / "BRIEF.md")
    shutil.copy(REPO_ROOT / "tools" / "openapi.json", d / "service-contract.json")
    shutil.copy(REPO_ROOT / "public-test-set.json", d / "people.json")
    return d
def test_brief_md_first_and_content_classification(tmp_path: Path) -> None:
    d = _folder(tmp_path)
    got = discover(d)
    assert [p.name for p in got.briefs] == ["BRIEF.md", "zzz-notes.md"]
    assert got.openapi_path == d / "service-contract.json"
    assert got.customers_path == d / "people.json"
    assert got.log
def test_corrupt_and_binary_files_are_skipped(tmp_path: Path) -> None:
    d = _folder(tmp_path)
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    (d / "blob.md").write_bytes(b"\x00\x01\x02\xff")
    (d / "image.png").write_bytes(b"\x89PNG\x00\x00")
    got = discover(d)
    skipped = {p.name for p, _ in got.skipped}
    assert {"broken.json", "blob.md", "image.png"} <= skipped
    assert got.openapi_path is not None
    assert [p.name for p in got.briefs] == ["BRIEF.md", "zzz-notes.md"]
def test_no_contract_is_reported(tmp_path: Path) -> None:
    d = _folder(tmp_path)
    (d / "service-contract.json").unlink()
    got = discover(d)
    assert got.openapi_path is None
    assert any("contract: none" in line for line in got.log)
def test_unknown_txt_and_readme_become_extras(tmp_path: Path) -> None:
    d = _folder(tmp_path)
    (d / "memo.txt").write_text("hello", encoding="utf-8")
    (d / "README.md").write_text("what is what", encoding="utf-8")
    got = discover(d)
    assert {p.name for p in got.extras} == {"memo.txt", "README.md"}
    text = extra_text(d / "memo.txt")
    assert text is not None and text.startswith("# EXTRA MATERIAL: memo.txt")
def test_classify_json_shapes() -> None:
    assert classify_json({"openapi": "3.0.0", "paths": {"/a": {}}}) == "openapi"
    assert classify_json([{"customer_id": "c1", "identity": {}, "products": []}]) == "customers"
    assert classify_json({"customers": [{"id": "c1", "products": []}]}) == "customers"
    assert classify_json([{"x": 1}]) == "data"
    assert classify_json("text") == "data"
def test_missing_folder_does_not_raise(tmp_path: Path) -> None:
    got = discover(tmp_path / "nope")
    assert got.briefs == []
