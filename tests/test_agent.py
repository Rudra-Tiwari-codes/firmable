"""The agent end to end, offline: only the model and CKAN are faked. Real files, real engine, real state."""

import csv
import json
import re

import pytest
import yaml

from firmable import agent, inspect_tools, llm, review
from firmable.inspect_tools import reservoir_sample, split_records

ABN = "95000029729"


def _pkg(url):
    return {"id": "pkg-1", "name": "test-register", "title": "Test register", "license_id": "cc-by", "license_title": "CC BY",
            "organization": {"title": "Test Dept"}, "notes": "A register.", "metadata_modified": "2026-01-01",
            "resources": [{"id": "res-1", "name": "data", "format": "CSV", "url": url, "last_modified": "2026-01-01"}]}


class FakeCKAN:
    pkg = None

    def package_show(self, d):
        return self.pkg


class FakeModel:
    """Scripted replies, and a copy of every prompt it was sent."""

    def __init__(self, first_proposal):
        self.prompts = {}
        self.proposal = first_proposal
        self.revise_replies = []

    def __call__(self, step, system, user, effort="medium", max_tokens=16000):
        self.prompts.setdefault(step, []).append(user)
        usage = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0,
                 "cache_read_input_tokens": 0, "calls": 1, "model": "fake", "seconds": 0.0, "usd": 0.01}
        if step == "select_resource":
            return {"resource_id": "res-1", "fallback_ids": [], "reason": "only one"}, dict(usage)
        if step == "inspect":
            return {"what_it_is": "a csv", "record_grain": "one licensee", "contains_business_entities": True,
                    "evidence": "columns", "reader": {"format": "csv", "encoding": "utf-8"}, "concerns": []}, dict(usage)
        if step == "propose":
            return self.proposal, dict(usage)
        if step == "revise":
            return (self.revise_replies.pop(0) if self.revise_replies else
                    {"no_changes": True, "changes": []}), dict(usage)
        raise AssertionError(f"unexpected step {step}")


def _write_source(workspace, n=6000):
    """Names are unique per row, so a value in a prompt can be traced back to the row it came from."""
    path = workspace / "raw" / "res-1.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Name", "ABN"])
        for i in range(n):
            w.writerow([f"ROWMARK{i:05d} PTY LTD", ABN])
    (workspace / "raw" / "res-1.meta.json").write_text(json.dumps(
        {"file": path.name, "sha256": review.file_sha256(path), "truncated": False, "url": "http://x"}), encoding="utf-8")
    return path


def _good_body():
    return {"record_id": {"strategy": "row_hash"},
            "mappings": [{"canonical": "entity.legal_name", "source": "Name", "confidence": 0.9, "rationale": "r"},
                         {"canonical": "entity.abn", "source": "ABN", "transforms": [{"op": "abn"}], "confidence": 0.9, "rationale": "r"}],
            "source_reliability": {"value": 0.9, "rationale": "r"}, "unmapped_source_fields": [], "unmapped_canonical_fields": []}


@pytest.fixture
def harness(workspace, monkeypatch):
    FakeCKAN.pkg = _pkg("http://example.invalid/data.csv")
    _write_source(workspace)
    model = FakeModel(_good_body())
    monkeypatch.setattr(agent, "CKAN", FakeCKAN)
    monkeypatch.setattr(llm, "call_json", model)
    return workspace, model


def test_splits_do_not_overlap_and_small_files_still_get_every_set():
    for n in (126, 5000):
        s = split_records(list(range(n)))
        parts = [set(s[k]) for k in ("sample_idx", "holdout_idx", "final_test_idx", "profile_idx")]
        assert parts[0] and parts[1] and parts[2]
        assert not parts[1] & parts[2] and not parts[1] & parts[3] and not parts[2] & parts[3]
    big = split_records(list(range(5000)))
    assert (len(big["sample_idx"]), len(big["holdout_idx"]), len(big["final_test_idx"])) == (25, 500, 500)


class _FakeResponse:
    status_code = 200
    url = "http://x/y.csv"
    headers = {}

    def raise_for_status(self):
        pass

    def iter_content(self, n):
        yield b"a,b\n1,2\n"


def test_download_retries_a_dropped_connection_but_not_a_404(tmp_path, monkeypatch):
    import requests
    monkeypatch.setattr(inspect_tools, "RAW_DIR", tmp_path)
    monkeypatch.setattr(inspect_tools.time, "sleep", lambda s: None)
    calls = []

    def flaky(url, **k):
        calls.append(url)
        if len(calls) < 3:
            raise requests.ConnectionError("Remote end closed connection")
        return _FakeResponse()

    monkeypatch.setattr(inspect_tools.requests, "get", flaky)
    path, meta = inspect_tools.download("http://x/y.csv", "res")
    assert len(calls) == 3 and meta["bytes"] == 8 and path.read_bytes() == b"a,b\n1,2\n"

    calls.clear()
    not_found = requests.HTTPError(response=type("R", (), {"status_code": 404})())

    def missing(url, **k):
        calls.append(url)
        raise not_found

    monkeypatch.setattr(inspect_tools.requests, "get", missing)
    with pytest.raises(requests.HTTPError):
        inspect_tools.download("http://x/z.csv", "res2")
    assert len(calls) == 1


def test_an_oversized_download_is_not_treated_as_the_whole_file(harness, monkeypatch):
    monkeypatch.setattr(agent, "download", lambda url, rid: ("x", {"truncated": True}))
    assert agent.onboard("test-register") is None


def test_a_tiny_file_still_keeps_rows_back_to_test_on():
    """A live 23-row XLSX put every row in the sample and "passed" validation on zero records."""
    s = split_records(list(range(23)))
    assert len(s["sample_idx"]) == 11 and s["holdout_idx"] and s["final_test_idx"]


def test_validating_on_nothing_is_not_a_pass():
    from firmable.validate import validate
    cfg = {"record_id": {"strategy": "row_hash"}, "source_reliability": {"value": 0.9},
           "mappings": [{"canonical": "entity.legal_name", "source": "n", "confidence": 0.9}]}
    report = validate(cfg, ["n"], [])
    assert not report["passed"] and "not been tested" in report["failing"][0]["error"]


def test_reservoir_sample_is_deterministic_and_reaches_the_whole_file():
    rows, total = reservoir_sample(iter(range(100000)), 5000)
    assert total == 100000 and len(rows) == 5000
    assert rows == reservoir_sample(iter(range(100000)), 5000)[0]
    assert max(rows) > 90000  # not just the top of the file
    skipped = reservoir_sample(iter(range(100000)), 5000, skip=25)[0]
    assert min(skipped) >= 25, "rows the inspect step showed the model must not be sampled"
    assert sorted(reservoir_sample(iter(range(30)), 5000, skip=25)[0]) == list(range(30)), "a tiny file keeps every row"


def test_full_run_writes_a_pending_config_and_never_leaks_holdout_or_final_test(harness):
    workspace, model = harness
    assert agent.onboard("test-register") == "test-register"

    state = workspace / "runs" / "test-register"
    profile = json.loads((state / "profile.json").read_text(encoding="utf-8"))
    records = agent.sample_rows(workspace / "raw" / "res-1.csv", {"format": "csv"})[0]
    holdout = {records[i]["Name"][:12] for i in profile["holdout_idx"]}
    final = {records[i]["Name"][:12] for i in profile["final_test_idx"]}
    assert len(holdout) == 500 and len(final) == 500

    propose = model.prompts["propose"][0]
    seen_in_propose = set(re.findall(r"ROWMARK\d{5}", propose))
    assert seen_in_propose, "the prompt should show some rows"
    assert not seen_in_propose & holdout, "holdout rows reached the propose prompt"
    assert not seen_in_propose & final, "final-test rows reached the propose prompt"

    everything_the_model_saw = " ".join(p for ps in model.prompts.values() for p in ps)
    assert not set(re.findall(r"ROWMARK\d{5}", everything_the_model_saw)) & final, "final-test rows reached the model"

    assert profile["records_in_file"] == 6000
    cfg = yaml.safe_load((workspace / "configs" / "test-register.yaml").read_text(encoding="utf-8"))
    assert cfg["review"]["status"] == "pending"
    assert cfg["agent_run"]["final_test_passed"] is True
    packet = (workspace / "outputs" / "review" / "test-register.md").read_text(encoding="utf-8")
    assert "Final test" in packet and "the model never saw" in packet


def test_a_failing_mapping_is_revised_by_the_model(harness):
    workspace, model = harness
    bad = _good_body()
    bad["mappings"][1]["transforms"] = [{"op": "acn"}]  # ABN column parsed as an ACN: fails on every row
    model.proposal = bad
    model.revise_replies = [{"no_changes": False, "changes": [{"what": "use abn", "why": "acn failed"}], "config": _good_body()}]
    assert agent.onboard("test-register")
    cfg = yaml.safe_load((workspace / "configs" / "test-register.yaml").read_text(encoding="utf-8"))
    assert cfg["agent_run"]["config_version"] == 2 and cfg["agent_run"]["validation_passed"]
    assert cfg["review"]["status"] == "pending"
    assert "acn" in model.prompts["revise"][0].lower()


def test_a_config_still_failing_is_marked_and_cannot_be_approved_silently(harness):
    workspace, model = harness
    bad = _good_body()
    bad["mappings"][1]["transforms"] = [{"op": "acn"}]
    model.proposal = bad
    model.revise_replies = [{"no_changes": False, "changes": [], "config": bad}] * 3
    agent.onboard("test-register")
    cfg = yaml.safe_load((workspace / "configs" / "test-register.yaml").read_text(encoding="utf-8"))
    assert cfg["review"]["status"] == "pending_needs_attention"
    with pytest.raises(SystemExit):
        review.review("test-register", approve=True, reviewer="me")
    review.review("test-register", approve=True, reviewer="me", note="accepted because reasons")
    assert review.is_approved(review.load_config("test-register"))


def test_failure_keeps_state_and_a_rerun_resumes_without_repeating_paid_steps(harness, monkeypatch):
    workspace, model = harness
    real = llm.call_json

    def dies_on_propose(step, *a, **k):
        if step == "propose":
            raise llm.LLMError("boom", usage={"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0,
                                              "cache_read_input_tokens": 0, "usd": 0.5})
        return real(step, *a, **k)

    monkeypatch.setattr(llm, "call_json", dies_on_propose)
    assert agent.onboard("test-register") is None
    rows = list(csv.DictReader(open(workspace / "outputs" / "onboarding_costs.csv", encoding="utf-8")))
    assert rows[0]["status"] == "failed at propose"
    assert float(rows[0]["usd"]) >= 0.5, "a billed failure has to appear in the ledger"

    calls_before = len(model.prompts["inspect"])
    monkeypatch.setattr(llm, "call_json", model)
    assert agent.onboard("test-register") == "test-register"
    assert len(model.prompts["inspect"]) == calls_before, "resume repeated a finished step"
    rows = list(csv.DictReader(open(workspace / "outputs" / "onboarding_costs.csv", encoding="utf-8")))
    assert len(rows) == 1 and rows[0]["status"] == "finalized"


def test_human_feedback_reaches_the_model_and_resets_approval_without_duplicating_cost(harness):
    workspace, model = harness
    assert agent.onboard("test-register")
    review.review("test-register", approve=True, reviewer="me")
    assert review.is_approved(review.load_config("test-register"))

    changed = _good_body()
    changed["mappings"][0]["confidence"] = 0.4
    model.revise_replies = [{"no_changes": False, "changes": [{"what": "lower", "why": "feedback"}], "config": changed}]
    review.review("test-register", feedback="legal_name confidence is too high")

    assert "legal_name confidence is too high" in model.prompts["revise"][-1]
    cfg = review.load_config("test-register")
    assert cfg["review"]["status"] == "pending"
    assert not review.is_approved(cfg)
    assert cfg["mappings"][0]["confidence"] == 0.4
    rows = list(csv.DictReader(open(workspace / "outputs" / "onboarding_costs.csv", encoding="utf-8")))
    assert len(rows) == 1


def test_reader_repair_loop_fixes_a_wrong_encoding(harness, monkeypatch):
    workspace, model = harness
    path = workspace / "raw" / "res-1.csv"
    path.write_bytes("Name,ABN\nCaf\xe9 PTY LTD,95000029729\n".encode("cp1252") * 300)
    base = model.__call__

    def scripted(step, *a, **k):
        if step == "inspect":
            obj, usage = base(step, *a, **k)
            obj["reader"]["encoding"] = "utf-8"
            return obj, usage
        if step == "repair_reader":
            model.prompts.setdefault(step, []).append(a[1])
            return {"reader": {"format": "csv", "encoding": "cp1252"}, "diagnosis": "not utf-8"}, {
                "input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                "calls": 1, "model": "fake", "seconds": 0, "usd": 0.0}
        return base(step, *a, **k)

    monkeypatch.setattr(llm, "call_json", scripted)
    assert agent.onboard("test-register")
    assert "could not decode" in model.prompts["repair_reader"][0].lower() or "decode" in model.prompts["repair_reader"][0].lower()
    assert review.load_config("test-register")["reader"]["encoding"] == "cp1252"


def test_licence_other_is_flagged_on_new_configs(harness):
    FakeCKAN.pkg = {**FakeCKAN.pkg, "license_id": "other", "license_title": "Other"}
    workspace, model = harness
    agent.onboard("test-register")
    assert review.load_config("test-register")["source"]["licence_flag"]
    assert "Licence" in (workspace / "outputs" / "review" / "test-register.md").read_text(encoding="utf-8")
