"""Link rules one at a time, then the whole extract -> link -> profile path on small synthetic sources."""

import csv
import json
from collections import Counter

import pytest
import yaml

from firmable import audit, engine, link, profile, review
from firmable.link import Clusters, SourceEntity, entity_key, looks_like_person, wilson_interval

ABN_A, ABN_B = "95000029729", "51824753556"
ACN_A, ACN_B = "000029729", "004085616"


def se(source, key, *names, legal=None, abn=None, acn=None, postcode=None, state=None, etype=None, records=1):
    s = SourceEntity(source, key)
    for i in range(records):
        s.add(f"{key}-{i}", {"names": list(names), "legal": list(names if legal is None else legal), "abn": abn, "acn": acn,
                             "postcode": postcode, "state": state, "locality": None, "full": None, "entity_type": etype})
    s.finish()
    return s


def score(a, b):
    counts = Counter((x.source_id, n) for x in (a, b) for n in x.norms)
    return link.score(a, b, counts)


def test_exact_abn_with_matching_names_is_confident():
    c = score(se("a", "1", "ACME PTY LTD", abn=ABN_A), se("b", "2", "Acme Limited", abn=ABN_A))
    assert c["tier"] == "abn_exact" and c["confidence"] == 0.99 and not c["refuse_reason"]


def test_exact_abn_with_different_names_is_lower_but_accepted():
    c = score(se("a", "1", "Silverchain Care Pty Ltd", abn=ABN_A), se("b", "2", "KINCARE HEALTH SERVICES PTY LTD", abn=ABN_A))
    assert c["confidence"] == 0.85 and not c["refuse_reason"]


def test_person_under_an_employers_abn_is_refused():
    c = score(se("a", "1", "The University Of Queensland", abn=ABN_A), se("b", "2", "Rion George SENIOR", abn=ABN_A))
    assert c["confidence"] < link.THRESHOLD and "person" in c["refuse_reason"]


def test_abn_match_is_refused_when_no_name_agrees_and_that_abn_carries_unrelated_names():
    c = score(se("a", "1", "Oldfield Charity Limited", abn=ABN_A),
              se("b", "2", "ABBOTT AUSTRALASIA PTY LTD", "AECOM SERVICES PTY LTD", abn=ABN_A))
    assert c["confidence"] < link.THRESHOLD and "unrelated names" in c["refuse_reason"]


def test_same_name_different_abns_is_refused_outright():
    c = score(se("a", "1", "ACME PTY LTD", abn=ABN_A, postcode="2000"), se("b", "2", "ACME PTY LTD", abn=ABN_B, postcode="2000"))
    assert c["confidence"] == 0.0


def test_same_name_different_acns_is_refused_outright():
    c = score(se("a", "1", "ACME PTY LTD", acn=ACN_A), se("b", "2", "ACME PTY LTD", acn=ACN_B))
    assert c["confidence"] == 0.0


def test_name_and_postcode_links_when_nothing_argues_against_it():
    c = score(se("a", "1", "Urunga Surf Life Saving Club Inc", abn=ABN_A, postcode="2455"),
              se("b", "2", "URUNGA SURF LIFE SAVING CLUB INC", postcode="2455"))
    assert c["tier"] == "name" and c["confidence"] == 0.85 and c["evidence"]["matched_via"] == "legal_names"


def test_name_alone_without_location_does_not_link():
    c = score(se("a", "1", "Urunga Surf Life Saving Club", abn=ABN_A, postcode="2455", state="NSW"),
              se("b", "2", "Urunga Surf Life Saving Club", postcode="9999", state="NSW"))
    assert c["confidence"] < link.THRESHOLD


def test_venue_named_after_an_organisation_does_not_merge_with_it():
    """The University of Newcastle was merged with NUSERVICES PTY LTD through a premises name."""
    uni = se("acnc", "1", "The University Of Newcastle", abn=ABN_A, postcode="2308")
    venue = se("nsw", "2", "NUSERVICES PTY LTD", "University of Newcastle", legal=["NUSERVICES PTY LTD"], postcode="2308")
    c = score(uni, venue)
    assert c["confidence"] < link.THRESHOLD and "legal names differ" in c["refuse_reason"]


def test_match_on_a_trading_name_when_only_one_side_states_a_legal_name_is_lower_but_accepted():
    charity = se("acnc", "1", "Hungry Head Club Inc", abn=ABN_A, postcode="2455")
    venue = se("nsw", "2", "Hungry Head Club Inc", legal=[], postcode="2455")
    c = score(charity, venue)
    assert c["confidence"] == 0.82 and c["evidence"]["matched_via"] == "trading_or_premises_name"


def test_limited_and_incorporated_are_not_assumed_to_be_one_entity():
    c = score(se("a", "1", "FOUR WINDS CONCERTS LIMITED", abn=ABN_A, postcode="2546"),
              se("b", "2", "FOUR WINDS CONCERTS INCORPORATED", postcode="2546"))
    assert c["confidence"] < link.THRESHOLD and "legal forms" in c["refuse_reason"]


def test_common_names_are_refused():
    a, b = se("a", "1", "Royal Hotel", abn=ABN_A, postcode="2000"), se("b", "2", "Royal Hotel", postcode="2000")
    counts = Counter({("a", "ROYAL HOTEL"): 1, ("b", "ROYAL HOTEL"): 9})
    c = link.score(a, b, counts)
    assert c["confidence"] < link.THRESHOLD and "common name" in c["refuse_reason"]


def test_acn_tier_and_bridge_tier_work_when_data_reaches_them():
    c = score(se("a", "1", "ACME PTY LTD", acn=ACN_A), se("b", "2", "ACME PTY LTD", acn=ACN_A))
    assert c["tier"] == "acn_exact" and c["confidence"] == 0.98
    c = score(se("a", "1", "ACME PTY LTD", abn=ABN_A), se("b", "2", "ACME PTY LTD", acn=ABN_A[2:]))
    assert c["tier"] == "abn_acn_bridge" and c["confidence"] == 0.93
    c = score(se("a", "1", "ACME PTY LTD", abn=ABN_A), se("b", "2", "Unrelated Bakery", acn=ABN_A[2:]))
    assert c["confidence"] < link.THRESHOLD


@pytest.mark.parametrize("name,person", [("Jane Smith", True), ("GOYAL, PANKAJ", True), ("The Hub", False),
                                         ("House With No Steps", False), ("Transport Heritage NSW", False),
                                         ("Scripture Union Queensland", False), ("ACME PTY LTD", False), ("MONO", False)])
def test_person_detector(name, person):
    assert looks_like_person([name], set()) is person


def test_a_type_of_company_is_never_a_person():
    assert looks_like_person(["Jane Smith"], {"company"}) is False


def test_employees_under_an_abn_stay_out_of_the_employers_entity():
    s = SourceEntity("q", f"abn:{ABN_A}")
    claim = lambda n, t: {"names": [n], "legal": [n], "abn": ABN_A, "acn": None, "postcode": None, "state": None,
                          "locality": None, "full": None, "entity_type": t}
    s.add("r1", claim("THE UNIVERSITY OF QUEENSLAND", "unknown"))
    s.add("r2", claim("Rion George SENIOR", "unknown"))
    s.finish()
    assert s.records == ["r1"] and s.person_records == ["r2"]
    assert s.norms == {"UNIVERSITY OF QUEENSLAND"}


def test_a_company_name_misread_as_a_person_still_counts_as_abn_evidence():
    s = SourceEntity("act", f"abn:{ABN_A}")
    claim = lambda n: {"names": [n], "legal": [n], "abn": ABN_A, "acn": None, "postcode": None, "state": None,
                       "locality": None, "full": None, "entity_type": "unknown"}
    s.add("r1", claim("AMERICAN EXPRESS AUSTRALIA LIMITED"))
    s.add("r2", claim("BECTON DICKINSON"))
    s.finish()
    assert s.norms == {"AMERICAN EXPRESS AUSTRALIA"}
    c = score(s, se("qld", "2", "Becton Dickinson Pty Ltd", abn=ABN_A))
    assert c["confidence"] == 0.99


def test_an_abn_held_only_by_a_person_stays_that_person():
    s = SourceEntity("q", f"abn:{ABN_A}")
    s.add("r1", {"names": ["Jane Smith"], "legal": ["Jane Smith"], "abn": ABN_A, "acn": None, "postcode": None, "state": None,
                 "locality": None, "full": None, "entity_type": "unknown"})
    s.finish()
    assert s.records == ["r1"] and s.is_person_like()


def test_clusters_refuse_conflicting_abns_and_conflicting_acns():
    cl = Clusters()
    a, b, c = se("a", "1", "X", abn=ABN_A), se("b", "2", "X"), se("c", "3", "X", abn=ABN_B)
    assert cl.union(a, b) is None
    assert "different ABNs" in cl.union(b, c)
    cl = Clusters()
    a, b, c = se("a", "1", "X", acn=ACN_A), se("b", "2", "X"), se("c", "3", "X", acn=ACN_B)
    assert cl.union(a, b) is None
    assert "different ACNs" in cl.union(b, c)


def test_entity_keys_come_from_identifiers():
    assert entity_key([se("a", "1", "X", abn=ABN_A), se("b", "2", "X")]) == f"abn:{ABN_A}"
    assert entity_key([se("a", "1", "X", acn=ACN_A), se("b", "2", "X")]) == f"acn:{ACN_A}"
    key = entity_key([se("a", "1", "X"), se("b", "2", "X")])
    assert key.startswith("name:") and key == entity_key([se("b", "2", "X"), se("a", "1", "X")])


def test_link_ids_do_not_depend_on_pair_order():
    assert link.link_id("a", "b") == link.link_id("b", "a") != link.link_id("a", "c")


def test_wilson_interval():
    low, high = wilson_interval(48, 50)
    assert 0.85 < low < 0.96 < high <= 1
    assert wilson_interval(0, 0) is None


# --------------------------------------------------------------------------
# Whole pipeline on synthetic sources
# --------------------------------------------------------------------------


def _write_source(workspace, slug, rows, mappings):
    path = workspace / "raw" / f"{slug}.csv"
    cols = list(rows[0])
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    sha = review.file_sha256(path)
    (workspace / "raw" / f"{slug}.meta.json").write_text(json.dumps(
        {"file": path.name, "sha256": sha, "truncated": False}), encoding="utf-8")
    cfg = {"source_id": f"t:{slug}:0", "source": {"title": slug, "dataset_name": slug, "publisher": "p", "resource_id": slug,
                                                  "resource_url": f"http://x/{slug}.csv", "licence": "CC BY", "licence_id": "cc-by",
                                                  "observed_at_default": "2026-01-01", "raw_sha256": sha},
           "reader": {"format": "csv"}, "record_id": {"strategy": "row_hash"},
           "source_reliability": {"value": 0.9}, "mappings": mappings}
    cfg["review"] = {"status": "approved", "approved_config_hash": engine.config_hash(cfg), "reviewer": "test"}
    (workspace / "configs" / f"{slug}.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _m(canonical, source, *ops, conf=0.9):
    return {"canonical": canonical, "source": source, "confidence": conf, "transforms": [{"op": o} for o in ops]}


@pytest.fixture
def pipeline(workspace):
    charity_maps = [_m("entity.abn", "abn", "abn"), _m("entity.legal_name", "name"), _m("address.postcode", "pc", "postcode")]
    invoices_maps = [_m("entity.abn", "abn", "abn"), _m("entity.legal_name", "name", conf=0.5)]
    venues_maps = [_m("entity.legal_name", "licensee", conf=0.8), _m("entity.trading_name", "venue", conf=0.7),
                   _m("address.postcode", "pc", "postcode")]
    _write_source(workspace, "charities", [
        {"abn": ABN_A, "name": "Oz Rescue Limited", "pc": "2000"},
        {"abn": ABN_B, "name": "University Of Testville", "pc": "2308"},
    ], charity_maps)
    _write_source(workspace, "invoices", [
        {"abn": ABN_A, "name": "OZ RESCUE LTD"},
        {"abn": ABN_A, "name": "OZ RESCUE LTD"},
        {"abn": "12345678901", "name": "BAD CHECKSUM PTY LTD"},
    ], invoices_maps)
    _write_source(workspace, "venues", [
        {"licensee": "TESTVILLE SERVICES PTY LTD", "venue": "University Of Testville", "pc": "2308"},
        {"licensee": "Oz Rescue Limited", "venue": "Oz Rescue Limited", "pc": "2000"},
    ], venues_maps)
    return workspace


def test_pipeline_links_the_right_pairs_refuses_the_wrong_one_and_is_repeatable(pipeline, capsys):
    summary = link.run()
    assert summary["all_configs_approved"] and not summary["any_source_drifted"]
    links = [json.loads(l) for l in open(link.LINK_DIR / "links.jsonl", encoding="utf-8")]
    pairs = {tuple(sorted((l["source_a_record"]["name"].upper(), l["source_b_record"]["name"].upper()))) for l in links}
    assert ("OZ RESCUE LIMITED", "OZ RESCUE LTD") in pairs
    assert not any("TESTVILLE SERVICES" in p for pair in pairs for p in pair)
    assert all(l["link_id"] and l["matcher_version"] == link.MATCHER_VERSION and l["extractor_versions"] for l in links)

    queue = [json.loads(l) for l in open(link.LINK_DIR / "unlinked_queue.jsonl", encoding="utf-8")]
    assert any("legal names differ" in q["reason"] for q in queue)

    candidates = (link.LINK_DIR / "candidates.jsonl").read_text(encoding="utf-8")
    entities = (link.LINK_DIR / "entities.jsonl").read_text(encoding="utf-8")
    link.run()
    assert (link.LINK_DIR / "candidates.jsonl").read_text(encoding="utf-8") == candidates
    assert (link.LINK_DIR / "entities.jsonl").read_text(encoding="utf-8") == entities


def test_profile_has_per_field_confidence_provenance_and_no_blank_fields(pipeline, capsys):
    link.run()
    summary = profile.run(n=5, as_of="2026-11-06")
    profiles = [json.loads(l) for l in open(profile.PROFILE_DIR / "profiles.jsonl", encoding="utf-8")]
    assert summary["profiles"] == len(profiles) > 0
    p = next(x for x in profiles if x["canonical_entity_key"] == f"abn:{ABN_A}")
    name = p["fields"]["legal_name"]
    assert 0 < name["confidence"] <= 1 and name["provenance"]["records"]
    assert set(name["confidence_components"]) == {"mapping", "source_reliability", "link", "support", "agreement"}
    assert "disagreements" not in name  # Limited / LTD are spellings, not a disagreement
    assert len(name["spellings_merged"]) > 1
    assert "website" not in p["fields"]
    assert p["as_of"] == "2026-11-06"
    assert (profile.PROFILE_DIR / "SOURCE_DELETION.md").exists()


def test_disagreements_show_the_winner_the_losers_and_why():
    obs = lambda v, s, c: {"value": v, "source_id": s, "source_record_id": "r", "field_confidence": c, "source_reliability": 0.9,
                           "link_confidence": 0.99, "observed_at": "2026", "field": "entity.legal_name"}
    r = profile.resolve_scalar("legal_name", [obs("ACME PTY LTD", "a", 0.9), obs("ACME PTY LTD", "b", 0.9), obs("VANTIVE", "c", 0.5)])
    assert r["value"] == "ACME PTY LTD"
    assert r["disagreements"][0]["value"] == "VANTIVE" and "lower support" in r["disagreements"][0]["why_lost"]
    unanimous = profile.resolve_scalar("legal_name", [obs("ACME PTY LTD", "a", 0.9), obs("ACME PTY LTD", "b", 0.9)])
    assert r["confidence"] < unanimous["confidence"]


def test_reordered_names_are_one_value_and_unknown_type_abstains():
    obs = lambda v, s, f="entity.legal_name": {"value": v, "source_id": s, "source_record_id": "r", "field_confidence": 0.9,
                                               "source_reliability": 0.9, "link_confidence": 0.99, "observed_at": "2026", "field": f}
    r = profile.resolve_scalar("legal_name", [obs("GOYAL, PANKAJ", "a"), obs("PANKAJ GOYAL", "b")])
    assert "disagreements" not in r
    assert profile.resolve_scalar("entity_type", [obs("unknown", "a")]) is None


def test_expired_claims_weigh_less_against_the_as_of_date(monkeypatch):
    o = {"field_confidence": 1, "source_reliability": 1, "link_confidence": 1, "valid_to": "2025-01-01"}
    monkeypatch.setattr(profile, "AS_OF", "2024-01-01")
    assert profile._weight(o) == 1
    monkeypatch.setattr(profile, "AS_OF", "2026-01-01")
    assert profile._weight(o) == 0.5


def test_unapproved_configs_are_skipped_and_a_run_with_none_says_so(pipeline, capsys):
    for p in (pipeline / "configs").glob("*.yaml"):
        cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
        cfg["review"] = {"status": "pending"}
        p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(SystemExit):
        link.run()


def test_source_change_is_recorded_not_just_printed(pipeline, capsys):
    raw = pipeline / "raw" / "invoices.csv"
    raw.write_text(raw.read_text(encoding="utf-8") + f"{ABN_A},OZ RESCUE LTD\n", encoding="utf-8")
    summary = link.run()
    assert summary["any_source_drifted"]
    assert "differs from the one the config was approved" in capsys.readouterr().out


def test_sheets_follow_the_links_until_someone_answers_then_are_protected(pipeline, capsys):
    link.run()
    sheet = link.CHECKS / "links_reviewed.csv"
    rows = link.read_sheet(sheet)
    assert rows and rows[0]["link_id"]
    rows[0][link.VERDICT_COL] = "y"
    with open(sheet, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    link.run()
    assert link.read_sheet(sheet)[0][link.VERDICT_COL] == "y"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "cp1252"])
def test_report_reads_a_sheet_saved_by_a_spreadsheet_and_logs_each_decision(pipeline, capsys, encoding):
    link.run()
    sheet = link.CHECKS / "links_reviewed.csv"
    rows = link.read_sheet(sheet)
    for r in rows:
        r[link.VERDICT_COL] = "y"
        r["notes"] = "Café"
    with open(sheet, "w", newline="", encoding=encoding) as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    out = link.report(reviewer="Rudra")
    assert out["links_reviewed"]["precision"] == 1.0 and out["links_reviewed"]["checked"] == len(rows)
    assert out["links_reviewed"]["ci95"]
    decisions = [json.loads(l) for l in open(link.LINK_DIR / "decisions.jsonl", encoding="utf-8")]
    assert decisions[0]["reviewer"] == "Rudra" and decisions[0]["verdict"] == "y" and decisions[0]["matcher_version"]


def test_report_ignores_a_verdict_on_a_link_whose_score_moved(pipeline, capsys):
    link.run()
    sheet = link.CHECKS / "links_reviewed.csv"
    rows = link.read_sheet(sheet)
    rows[0][link.VERDICT_COL] = "y"
    rows[0]["confidence"] = "0.5"
    with open(sheet, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    assert not link.report()


def test_new_matcher_keeps_the_old_links_and_diff_reports_the_change(pipeline, capsys, monkeypatch):
    link.run()
    monkeypatch.setattr(link, "MATCHER_VERSION", "rules-9.9.9")
    original = link.score

    def stricter(a, b, counts):
        c = original(a, b, counts)
        if c["tier"] == "name":
            c["confidence"], c["refuse_reason"] = 0.2, "rules-9.9.9 refuses all name-only links"
        return c

    monkeypatch.setattr(link, "score", stricter)
    link.run()
    old = link.LINK_DIR / "history" / "links.rules-0.2.0.jsonl"
    assert old.exists()
    diff = link.diff_links(old)
    assert diff["removed"] >= 1 and diff["added"] == 0
    assert diff["removed_links"][0]["why"] == "rules-9.9.9 refuses all name-only links"
    assert diff["new_matcher"] == "rules-9.9.9"


def test_audit_checks_the_whole_file_and_flags_what_a_sample_would_miss(pipeline, monkeypatch):
    monkeypatch.setattr(audit, "AGENT_WINDOW", 1)
    results = {r["slug"]: r for r in audit.run()}
    inv = results["invoices"]
    assert inv["records_in_file"] == 3 and inv["rows_beyond_agent_window"] == 2
    assert inv["whole_file"]["worst_error_rate"] > 0.3
    assert inv["beyond_agent_window"]["mappings_over_error_limit"] == ["entity.abn"]
    md = (audit.AUDIT_DIR / "config_audit.md").read_text(encoding="utf-8")
    assert "invoices" in md and "over the 5% error limit" in md.lower()
