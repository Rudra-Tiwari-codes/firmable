from firmable.validate import MAX_ERROR_RATE, semantic, structural, validate

ABN = "95000029729"
ABN2 = "51824753556"


def _cfg(mappings, **kw):
    return {"record_id": {"strategy": "row_hash"}, "source_reliability": {"value": 0.9}, "mappings": mappings, **kw}


def _map(canonical="entity.legal_name", source="n", **kw):
    return {"canonical": canonical, "source": source, "confidence": 0.9, **kw}


def test_structural_catches_unknown_fields_columns_and_transforms():
    issues = structural(_cfg([_map("entity.nonsense"), _map(source="missing"),
                              _map(transforms=[{"op": "reverse"}])]), ["n"])
    text = " ".join(i["problem"] for i in issues)
    assert "unknown canonical" in text and "not in data" in text and "unknown transform" in text


def test_structural_catches_nested_args_that_the_engine_would_silently_ignore():
    issues = structural(_cfg([_map(transforms=[{"op": "date", "args": {"formats": ["%d/%m/%Y"]}}])]), ["n"])
    assert any("unknown argument" in i["problem"] for i in issues)


def test_structural_catches_missing_args_and_bad_regex():
    issues = structural(_cfg([_map(transforms=[{"op": "zfill"}]), _map(transforms=[{"op": "regex_extract", "pattern": "("}]),
                              _map(transforms=[{"op": "regex_extract", "pattern": "a", "group": 2}])]), ["n"])
    text = " ".join(i["problem"] for i in issues)
    assert "missing required" in text and "invalid regex" in text and "group 2" in text


def test_structural_passes_a_good_config():
    assert structural(_cfg([_map(transforms=[{"op": "zfill", "width": 9}])]), ["n"]) == []


def test_high_error_rate_fails():
    cfg = _cfg([_map("entity.abn", "a", transforms=[{"op": "abn"}])])
    report = validate(cfg, ["a"], [{"a": "12345678901"}] * 10)
    assert not report["passed"] and report["failing"][0]["error_rate"] > MAX_ERROR_RATE


def test_making_a_failing_pattern_optional_does_not_hide_from_the_report():
    """NSW's agent run passed by turning a failing regex optional. Error rate 0, 63% of cells gone."""
    cfg = _cfg([_map(transforms=[{"op": "regex_extract", "pattern": "PTY LTD$", "required": False}])])
    holdout = [{"n": "ACME PTY LTD"}] * 3 + [{"n": "Jane Smith"}] * 7
    report = validate(cfg, ["n"], holdout)
    assert report["passed"]
    assert report["empirical"]["per_mapping"][0]["silent_drop_rate"] == 0.7
    assert any("70%" in w for w in report["warnings"])


def test_semantic_flags_one_abn_with_unrelated_names():
    """The ACT case: every ABN passes its checksum, and it still names different businesses."""
    cfg = _cfg([_map(), _map("entity.abn", "a", transforms=[{"op": "abn"}])])
    rows = [{"n": "ABBOTT AUSTRALASIA", "a": ABN}, {"n": "AECOM SERVICES", "a": ABN}, {"n": "ZZ TOP PLUMBING", "a": ABN2}]
    sem = semantic(cfg, rows)
    assert sem["identifiers_with_unrelated_names"] == 1
    assert sem["warnings"]
    report = validate(cfg, ["n", "a"], rows, semantic_records=rows)
    assert report["passed"] and report["warnings"]  # a warning for the human, not a failure


def test_semantic_is_quiet_when_names_are_spellings_of_one_name():
    cfg = _cfg([_map(), _map("entity.abn", "a", transforms=[{"op": "abn"}])])
    sem = semantic(cfg, [{"n": "ACME PTY LTD", "a": ABN}, {"n": "ACME LIMITED", "a": ABN}])
    assert sem["identifiers_with_unrelated_names"] == 0 and not sem["warnings"]


def test_semantic_says_when_it_could_not_check():
    assert semantic(_cfg([_map()]), [{"n": "x"}])["checked"] is False
