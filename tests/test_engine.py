import io
import json
import zipfile

import openpyxl
import pytest

from firmable import engine
from firmable.names import legal_form, norm_name, same_name_variant
from firmable.review import is_approved

BOC_ABN = "95000029729"


def test_abn_checksum():
    assert engine.abn_valid(BOC_ABN)
    assert not engine.abn_valid("95000029728")
    assert not engine.abn_valid("123")


def test_acn_checksum():
    assert engine.acn_valid("000029729")
    assert not engine.acn_valid("000029728")


def test_transform_chain_routes_a_mixed_abn_acn_column():
    abn = [{"op": "digits_only"}, {"op": "length_in", "lengths": [11]}, {"op": "abn"}]
    acn = [{"op": "digits_only"}, {"op": "length_in", "lengths": [8, 9]}, {"op": "zfill", "width": 9}, {"op": "acn"}]
    assert engine.apply_transforms(BOC_ABN, abn) == [BOC_ABN]
    assert engine.apply_transforms(BOC_ABN, acn) == []
    assert engine.apply_transforms("29729", acn) == []  # too short for either
    assert engine.apply_transforms("8819458", abn) == []


def test_bad_checksum_raises_rather_than_emits():
    with pytest.raises(engine.TransformError):
        engine.apply_transforms("12345678901", [{"op": "abn"}])


@pytest.mark.parametrize("value,op,expected", [
    ("New South Wales", "state", "NSW"), ("800", "postcode", "0800"), ("www.Example.com/", "url", "http://www.example.com"),
    ("ACME PTY LTD", "entity_type_from_name", "company"), ("Rotary Club Inc", "entity_type_from_name", "incorporated_association"),
    ("Jane Smith", "entity_type_from_name", "unknown"), ("ACME PTY LTD ATF Smith Trust", "entity_type_from_name", "unknown"),
])
def test_single_transforms(value, op, expected):
    assert engine.apply_transforms(value, [{"op": op}]) == [expected]


def test_date_formats_and_excel_serial():
    assert engine.apply_transforms("31/01/2024", [{"op": "date", "formats": ["%d/%m/%Y"]}]) == ["2024-01-31"]
    assert engine.apply_transforms("45000", [{"op": "date", "formats": ["excel_serial"]}]) == ["2023-03-15"]


def _rows(fmt_bytes, reader):
    return list(engine._read_bytes(fmt_bytes, reader, None))


def test_csv_reader_header_row_and_cp1252():
    data = "A title row\nName,ABN\nCaf\xe9 Pty Ltd,1\n".encode("cp1252")
    rows = _rows(data, {"format": "csv", "encoding": "cp1252", "header_row": 1})
    assert rows == [{"Name": "Café Pty Ltd", "ABN": "1"}]


def test_tsv_reader():
    assert _rows(b"a\tb\n1\t2\n", {"format": "tsv"}) == [{"a": "1", "b": "2"}]


def test_xlsx_reader(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["notes above the table"])
    ws.append(["Name", "ABN", "When"])
    ws.append(["ACME PTY LTD", 95000029729.0, None])
    path = tmp_path / "x.xlsx"
    wb.save(path)
    rows = list(engine.read_records(path, {"format": "xlsx", "sheet": "Data", "header_row": 1}))
    assert rows == [{"Name": "ACME PTY LTD", "ABN": BOC_ABN, "When": ""}]


def test_xlsx_reader_can_read_a_table_split_across_sheets(tmp_path):
    wb = openpyxl.Workbook()
    wb.active.title = "2023"
    wb["2023"].append(["Name"])
    wb["2023"].append(["ACME"])
    wb.create_sheet("2024").append(["Name"])
    wb["2024"].append(["BETA"])
    path = tmp_path / "x.xlsx"
    wb.save(path)
    rows = list(engine.read_records(path, {"format": "xlsx", "sheet": "*"}))
    assert rows == [{"Name": "ACME", "_sheet": "2023"}, {"Name": "BETA", "_sheet": "2024"}]


def test_xml_reader_handles_namespaces_and_attributes():
    data = b'<r xmlns="urn:x"><Rec id="7"><Name>ACME</Name><Addr><City>Perth</City></Addr></Rec></r>'
    assert _rows(data, {"format": "xml", "record_tag": "Rec"}) == [{"@id": "7", "Name": "ACME", "City": "Perth"}]


def test_json_reader_records_path_and_flatten():
    data = json.dumps({"result": {"rows": [{"a": {"b": 1}, "c": ["x", "y"]}]}}).encode()
    assert _rows(data, {"format": "json", "records_path": "result.rows"}) == [{"a.b": "1", "c": "x | y"}]


def test_jsonl_reader():
    assert _rows(b'{"a": 1}\n\n{"a": 2}\n', {"format": "jsonl"}) == [{"a": "1"}, {"a": "2"}]


def test_zip_reader(tmp_path):
    path = tmp_path / "x.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("inner.csv", "a,b\n1,2\n")
    assert list(engine.read_records(path, {"format": "zip", "zip_member": "inner.csv"})) == [{"a": "1", "b": "2"}]


def test_unknown_format_is_a_reader_error():
    with pytest.raises(engine.ReaderError):
        _rows(b"x", {"format": "parquet"})


def _cfg(**kw):
    cfg = {"source_id": "s:1", "source": {"licence": "CC-BY", "observed_at_default": "2026-01-01"},
           "source_reliability": {"value": 0.9}, "record_id": {"strategy": "row_hash"},
           "mappings": [{"canonical": "entity.legal_name", "source": "n", "confidence": 0.9},
                        {"canonical": "entity.abn", "source": "a", "transforms": [{"op": "abn"}], "confidence": 0.9}]}
    cfg.update(kw)
    return cfg


def test_every_observation_has_the_full_envelope():
    errors = []
    obs = engine.observations_for({"n": "ACME", "a": BOC_ABN}, _cfg(), "r1", "2026-01-02T00:00:00+00:00", errors)
    required = {"source_id", "source_record_id", "observed_at", "ingested_at", "licence", "extractor_version",
                "field", "value", "field_confidence", "source_reliability", "raw_value", "source_field", "derivation_level"}
    assert len(obs) == 2 and not errors
    for o in obs:
        assert required <= set(o)
    assert "link_confidence" not in obs[0]  # the ontology: only on links


def test_contract_violation_is_dropped_and_counted():
    cfg = _cfg(mappings=[{"canonical": "entity.entity_type", "constant": "corporation", "confidence": 0.9}])
    errors = []
    assert engine.observations_for({}, cfg, "r1", "t", errors) == []
    assert "enum" in errors[0]["error"]


def test_a_bad_transform_argument_does_not_kill_the_run():
    cfg = _cfg(mappings=[{"canonical": "entity.legal_name", "source": "n",
                          "transforms": [{"op": "regex_extract", "pattern": "x", "group": 5}], "confidence": 0.9}])
    errors = []
    assert engine.observations_for({"n": "x"}, cfg, "r1", "t", errors) == []
    assert "IndexError" in errors[0]["error"]


def test_record_ids_are_deterministic_and_duplicates_get_a_suffix():
    cfg, seen = _cfg(), {}
    rec = {"n": "ACME"}
    first, second = engine.record_id(rec, cfg, seen), engine.record_id(dict(rec), cfg, seen)
    assert second == first + "#2"
    assert engine.record_id(rec, cfg, {}) == first


def test_config_hash_ignores_the_review_block_only():
    cfg = _cfg()
    h = engine.config_hash(cfg)
    assert engine.config_hash({**cfg, "review": {"status": "approved"}, "agent_run": {"usd": 1}}) == h
    changed = _cfg()
    changed["mappings"][0]["confidence"] = 0.1
    assert engine.config_hash(changed) != h


def test_is_approved_rejects_a_config_edited_after_approval():
    cfg = _cfg()
    cfg["review"] = {"status": "approved", "approved_config_hash": engine.config_hash(cfg)}
    assert is_approved(cfg)
    cfg["mappings"][0]["confidence"] = 0.1
    assert not is_approved(cfg)


@pytest.mark.parametrize("licence,flagged", [("cc-by", False), ("CC-BY-4.0", False), ("cc-by-4", False), ("cc-zero", False),
                                             ("other", True), ("other-closed", True), ("notspecified", True), (None, True), ("", True)])
def test_licence_flag(licence, flagged):
    assert bool(engine.licence_flag(licence)) == flagged


def test_norm_name_collapses_legal_spellings():
    assert norm_name("Oz Harvest Limited") == norm_name("OZ HARVEST LTD") == "OZ HARVEST"
    assert norm_name("The Trustee for Smith Family") == "SMITH FAMILY"
    assert norm_name("ACME PTY LTD T/A Bobs Burgers") == "ACME"


def test_legal_form_keeps_what_norm_name_drops():
    assert legal_form("Four Winds Concerts Limited") == "company"
    assert legal_form("Four Winds Concerts Incorporated") == "incorporated"
    assert legal_form("Four Winds Concerts") is None


@pytest.mark.parametrize("a,b,same", [("GOYAL PANKAJ", "PANKAJ GOYAL", True), ("T K ELEVATOR", "TK ELEVATOR", True),
                                      ("ROYAL AUSTRALIAN COLLEGE OF GENERAL PRACTITIONERS", "ROYAL AUSTRALIAN COLLEGE OF GENERAL PRACTICTIONERS", True),
                                      ("PHILIPS ELECTRONICS", "CAP SUPPORT", False), ("ACME", "", False)])
def test_same_name_variant(a, b, same):
    assert same_name_variant(a, b) is same
