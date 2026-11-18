"""The one generic extractor.

A mapping config (YAML, written by the agent, approved by a human) says how to
read a resource and how each source column becomes a canonical field. This
module knows nothing about any particular source: every per-source decision is
in the config. Transforms are a fixed vocabulary (TRANSFORMS below), so a config
is data, never code.

Output is one observation per (source record, canonical field, value), carrying
the full envelope from firmable_ontology.yaml.
"""

import csv
import datetime as dt
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

import yaml

from . import ONTOLOGY_PATH

# Stamped on every observation, together with a hash of the config (see extractor_version below).
# Bump it whenever a change to this file could change what the same config produces.
ENGINE_VERSION = "0.1.0"

# --------------------------------------------------------------------------
# Ontology
# --------------------------------------------------------------------------

_ONTOLOGY = None


def ontology():
    global _ONTOLOGY
    if _ONTOLOGY is None:
        _ONTOLOGY = yaml.safe_load(ONTOLOGY_PATH.read_text(encoding="utf-8"))
    return _ONTOLOGY


_CANONICAL = None


def canonical_fields():
    """Every mappable canonical field, as 'entity.abn', 'address.postcode', ...
    Cached: contract_violation asks for this once per emitted value."""
    global _CANONICAL
    if _CANONICAL is None:
        o = ontology()
        out = {}
        for section in ("entity", "address"):
            for name, spec in o[section].items():
                out[f"{section}.{name}"] = spec
        for name in ("observed_at", "valid_from", "valid_to"):
            out[f"observation.{name}"] = o["observation"][name]
        _CANONICAL = out
    return _CANONICAL


# Licence ids we treat as open, attribution-style terms. Anything else is a finding for the reviewer.
OPEN_LICENCE_PREFIXES = ("cc-by", "cc-zero", "cc0", "odc-", "ogl", "other-open", "other-pd")


def licence_flag(licence_id):
    """None if the catalogue gives a recognised open licence, otherwise why a human should read it."""
    lid = str(licence_id or "").strip().lower()
    if not lid or lid == "notspecified":
        return "no licence stated in the catalogue; treat as unknown"
    if any(lid.startswith(p) for p in OPEN_LICENCE_PREFIXES):
        return None
    return f"licence id {licence_id!r} is not a recognised open licence; read the terms before using this source"


# --------------------------------------------------------------------------
# Readers: path + reader config -> iterator of flat {column: str} dicts
# --------------------------------------------------------------------------


class ReaderError(Exception):
    pass


def read_records(path, reader, limit=None):
    """Entry point for reading any source. `reader` is the config's reader block (format, encoding,
    header_row, ...). Whatever the format, each record comes out as a flat dict of column -> text,
    so everything after this point is format-blind."""
    path = Path(path)
    fmt = (reader.get("format") or "").lower()
    # A zip is opened, the one member the config names is taken out, and that member is then read
    # like any other file, using inner_format or the member's own file extension.
    if fmt == "zip":
        with zipfile.ZipFile(path) as zf:
            member = reader.get("zip_member")
            if not member:
                raise ReaderError(f"zip needs zip_member; members: {zf.namelist()[:20]}")
            data = zf.read(member)
        inner = dict(reader, format=reader.get("inner_format") or Path(member).suffix.lstrip("."))
        yield from _read_bytes(data, inner, limit)
        return
    yield from _read_bytes(path.read_bytes(), reader, limit)


def _read_bytes(data, reader, limit):
    fmt = (reader.get("format") or "").lower()
    # Dispatch table: format name -> reader function. Each reader is a generator (it yields one
    # record at a time), so stopping at `limit` never parses rows that are not needed.
    readers = {"csv": _csv, "tsv": _csv, "txt": _csv, "xlsx": _xlsx, "json": _json,
               "jsonl": _jsonl, "geojson": _json, "xml": _xml}
    if fmt not in readers:
        raise ReaderError(f"unsupported format {fmt!r}; supported: {sorted(readers)}")
    n = 0
    for rec in readers[fmt](data, reader):
        yield rec
        n += 1
        if limit and n >= limit:
            return


def _decode(data, encoding=None):
    # Bytes -> text. With an encoding in the config, only that one is tried, and a wrong guess is an
    # error the agent has to repair. With none, common ones are tried in order: utf-8-sig (UTF-8,
    # dropping the invisible BOM mark Excel adds), then cp1252 (Windows), then latin-1, which
    # accepts any byte and so never fails.
    for enc in [encoding] if encoding else ["utf-8-sig", "cp1252", "latin-1"]:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise ReaderError("could not decode text")


def _csv(data, reader):
    text = _decode(data, reader.get("encoding"))
    # header_row = how many lines to skip before the header (title or notes lines above the table).
    lines = text.splitlines(keepends=True)[int(reader.get("header_row") or 0):]
    delim = reader.get("delimiter") or ("\t" if reader.get("format") == "tsv" else ",")
    rows = csv.reader(io.StringIO("".join(lines)), delimiter=delim)
    header = _dedupe_header(next(rows, []))
    if not header:
        raise ReaderError("empty header row")
    for row in rows:
        if not any(c.strip() for c in row):
            continue  # skip fully blank lines
        # Pair each header with its cell. A row shorter than the header gets "" for the missing cells.
        yield {h: (row[i].strip() if i < len(row) else "") for i, h in enumerate(header)}


def _dedupe_header(cells):
    """Make every column name usable as a dict key: a blank header cell becomes column_<position>,
    and a repeated name gets a numeric suffix (Address, Address_1, Address_2)."""
    out, seen = [], {}
    for i, c in enumerate(cells):
        c = (str(c).strip() if c is not None else "") or f"column_{i}"
        if c in seen:
            seen[c] += 1
            c = f"{c}_{seen[c]}"
        else:
            seen[c] = 0
        out.append(c)
    return out


def _cell(v):
    """Any spreadsheet or JSON value -> text, so all readers return strings. Excel stores whole
    numbers as floats, so a postcode arrives as 2000.0; the is_integer branch writes it back as "2000"."""
    if v is None:
        return ""
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _xlsx(data, reader):
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheet = reader.get("sheet")
    every = sheet == "*"  # one table split across sheets (e.g. one per year): rows carry a _sheet column
    if sheet and not every and sheet not in wb.sheetnames:
        raise ReaderError(f"sheet {sheet!r} not found; sheets: {wb.sheetnames}")
    sheets = wb.worksheets if every else [wb[sheet] if sheet else wb.worksheets[0]]
    for ws in sheets:
        rows = ws.iter_rows(values_only=True)
        for _ in range(int(reader.get("header_row") or 0)):
            next(rows, None)
        header = _dedupe_header(next(rows, []) or [])
        for row in rows:
            vals = [_cell(v) for v in row]
            if not any(vals):
                continue
            rec = {h: (vals[i] if i < len(vals) else "") for i, h in enumerate(header)}
            if every:
                rec["_sheet"] = ws.title
            yield rec


def _flatten(obj, prefix=""):
    """Nested JSON -> one flat dict with dotted keys, so JSON looks like a table to the rest of the
    engine. {"address": {"postcode": "2000"}} becomes {"address.postcode": "2000"}. A list of plain
    values is joined with " | "; a list of objects is numbered (contacts.0.name, contacts.1.name)."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}{k}."))
    elif isinstance(obj, list):
        if all(not isinstance(x, (dict, list)) for x in obj):
            out[prefix[:-1]] = " | ".join(_cell(x) for x in obj)
        else:
            for i, v in enumerate(obj):
                out.update(_flatten(v, f"{prefix}{i}."))
    else:
        out[prefix[:-1]] = _cell(obj)
    return out


def _walk(obj, path):
    # Follow a dotted records_path ("result.records") down into the JSON to the list of records.
    # An empty path means the top level of the file is already that list.
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(obj, list):
            obj = obj[int(part)]
        elif isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            raise ReaderError(f"records_path {path!r}: no key {part!r}")
    return obj


def _json(data, reader):
    obj = json.loads(_decode(data, reader.get("encoding")))
    recs = _walk(obj, reader.get("records_path"))
    if not isinstance(recs, list):
        raise ReaderError(f"records_path points at {type(recs).__name__}, not a list")
    for r in recs:
        yield _flatten(r) if isinstance(r, (dict, list)) else {"value": _cell(r)}


def _jsonl(data, reader):
    for line in _decode(data, reader.get("encoding")).splitlines():
        if line.strip():
            yield _flatten(json.loads(line))


def _xml(data, reader):
    import xml.etree.ElementTree as ET

    tag = reader.get("record_tag")
    if not tag:
        raise ReaderError("xml needs record_tag")
    found = False
    # iterparse walks the file element by element instead of building the whole tree in memory.
    # The "end" event fires when an element's closing tag is reached, so its children are complete.
    for _, el in ET.iterparse(io.BytesIO(data), events=("end",)):
        if _local(el.tag) != tag:
            continue
        found = True
        # One record element -> one flat dict: child tag -> text, attributes as "@name" or "tag@name",
        # and a repeated child tag gets _2, _3.
        rec = {f"@{_local(k)}": v for k, v in el.attrib.items()}
        for child in el.iter():
            if child is el:
                continue
            name = _local(child.tag)
            text = (child.text or "").strip()
            if text and len(child) == 0:
                key = name
                i = 1
                while key in rec:
                    i += 1
                    key = f"{name}_{i}"
                rec[key] = text
            for k, v in child.attrib.items():
                rec[f"{name}@{_local(k)}"] = v
        yield rec
        el.clear()  # free this element's memory once its record has been handed on
    if not found:
        raise ReaderError(f"no <{tag}> elements found")


def _local(tag):
    # ElementTree writes a namespaced tag as "{http://namespace}Name". Keep only "Name".
    return tag.split("}", 1)[-1]


# --------------------------------------------------------------------------
# Validators
# --------------------------------------------------------------------------


def abn_valid(s):
    """The official ABN check: subtract 1 from the first digit, multiply each of the 11 digits by
    its weight, add them up, and the total must divide by 89. A mistyped digit almost always
    breaks this. It proves the number is well-formed, not that it belongs to a particular business."""
    if not re.fullmatch(r"\d{11}", s or ""):
        return False
    d = [int(c) for c in s]
    d[0] -= 1
    w = [10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19]
    return sum(a * b for a, b in zip(d, w)) % 89 == 0


def acn_valid(s):
    """The official ACN check: weight the first 8 digits 8, 7, ... 1 and add them up. The 9th digit
    is a check digit and must equal (10 - total % 10) % 10."""
    if not re.fullmatch(r"\d{9}", s or ""):
        return False
    d = [int(c) for c in s]
    total = sum(a * b for a, b in zip(d[:8], range(8, 0, -1)))
    return (10 - total % 10) % 10 == d[8]


# --------------------------------------------------------------------------
# Transforms. Each takes (value, args) and returns a value, a list (fan-out),
# or None (no value). Raising TransformError marks a parse failure.
# --------------------------------------------------------------------------


class TransformError(Exception):
    pass


STATES = {
    "NSW": "NSW", "NEW SOUTH WALES": "NSW", "VIC": "VIC", "VICTORIA": "VIC",
    "QLD": "QLD", "QUEENSLAND": "QLD", "WA": "WA", "WESTERN AUSTRALIA": "WA",
    "SA": "SA", "SOUTH AUSTRALIA": "SA", "TAS": "TAS", "TASMANIA": "TAS",
    "ACT": "ACT", "AUSTRALIAN CAPITAL TERRITORY": "ACT", "NT": "NT",
    "NORTHERN TERRITORY": "NT", "NZ": "NZ", "NEW ZEALAND": "NZ",
}


def _t_date(v, a, with_time=False):
    s = str(v).strip()
    if not s:
        return None
    fmts = a.get("formats") or []
    # Excel stores a date as the number of days since 30 Dec 1899 (45000 is 15 Mar 2023). This is
    # only tried when the config asks for it, because a bare number could be something else.
    if "excel_serial" in fmts and re.fullmatch(r"\d{4,6}(\.\d+)?", s):
        d = dt.datetime(1899, 12, 30) + dt.timedelta(days=float(s))
        return d.isoformat() if with_time else d.date().isoformat()
    # Try each format the config lists, in order; the first one that parses wins. With none listed,
    # only ISO (2024-03-15) is accepted. Matching nothing is an error, never a guess.
    for f in [f for f in fmts if f != "excel_serial"] or ["iso"]:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if f == "iso" else dt.datetime.strptime(s, f)
            return d.isoformat() if with_time else d.date().isoformat()
        except ValueError:
            continue
    raise TransformError(f"date {s!r} matched none of {fmts or ['iso']}")


def _t_map(v, a):
    m = a.get("map") or {}
    key = str(v).strip()
    if a.get("case_insensitive", True):
        m = {str(k).strip().lower(): val for k, val in m.items()}
        key = key.lower()
    if key in m:
        return m[key]
    if "default" in a:
        return a["default"]
    raise TransformError(f"value {v!r} not in map")


def _t_abn(v, a):
    # Three outcomes, and every identifier transform below follows the same pattern:
    #   empty cell    -> None (no value, and not an error)
    #   bad checksum  -> TransformError (counted as a parse error; the value is dropped)
    #   valid         -> the cleaned digits
    s = re.sub(r"\D", "", str(v))
    if not s:
        return None
    if not abn_valid(s):
        raise TransformError(f"ABN {v!r} fails checksum")
    return s


def _t_acn(v, a):
    s = re.sub(r"\D", "", str(v))
    if not s:
        return None
    # Optional: derive an ACN from an ABN by dropping its 2 leading check digits. The result still
    # has to pass the ACN checksum below, which protects against ABNs that do not contain an ACN.
    if len(s) == 11 and a.get("from_abn") and abn_valid(s):
        s = s[2:]
    if not acn_valid(s):
        raise TransformError(f"ACN {v!r} fails checksum")
    return s


def _t_nzbn(v, a):
    s = re.sub(r"\D", "", str(v))
    if not s:
        return None
    if len(s) != 13:
        raise TransformError(f"NZBN {v!r} is not 13 digits")
    return s


def _t_state(v, a):
    s = re.sub(r"[^A-Z ]", "", str(v).upper()).strip()
    if not s:
        return None
    if s in STATES:
        return STATES[s]
    raise TransformError(f"state {v!r} not recognised")


def _t_postcode(v, a):
    s = re.sub(r"\D", "", str(v))
    if not s:
        return None
    if len(s) == 3:
        s = "0" + s  # NT/ACT postcodes lose their leading zero in spreadsheets
    if len(s) != 4:
        raise TransformError(f"postcode {v!r} is not 4 digits")
    return s


def _t_entity_type_from_name(v, a):
    """Only the unambiguous legal suffixes. Everything else stays unknown."""
    s = f" {str(v).upper()} "
    if re.search(r"\bPTY\.?\s*(LTD|LIMITED)\b|\bLIMITED\b|\bLTD\b", s) and not re.search(r"\bATF\b|\bTRUSTEE\b|\bAS TRUSTEE\b", s):
        return "company"
    if re.search(r"\bINC(ORPORATED)?\.?\s*$", s.rstrip()):
        return "incorporated_association"
    return "unknown"


def _t_regex(v, a):
    m = re.search(a["pattern"], str(v))
    if not m:
        if a.get("required"):
            raise TransformError(f"{v!r} does not match {a['pattern']!r}")
        return None
    return m.group(a.get("group", 0))


def _t_split(v, a):
    parts = [p.strip() for p in re.split(a.get("pattern") or re.escape(a.get("sep", ";")), str(v))]
    return [p for p in parts if p]


def _t_null_if(v, a):
    vals = {str(x).strip().lower() for x in a.get("values", ["", "-", "n/a", "na", "null", "none"])}
    return None if str(v).strip().lower() in vals else v


def _t_url(v, a):
    s = str(v).strip()
    if not s:
        return None
    if not re.match(r"^https?://", s, re.I):
        s = "http://" + s
    if not re.match(r"^https?://[^\s/]+\.[^\s/]+", s, re.I):
        raise TransformError(f"{v!r} is not a URL")
    return s.rstrip("/").lower()


# The whole vocabulary the agent can use: name -> (function, description). The descriptions are
# pasted into the agent's prompt, so the model is told exactly what exists. A config can only name
# these; it cannot bring code of its own. That is what makes a config data and safe to run.
TRANSFORMS = {
    "strip": (lambda v, a: str(v).strip(), "Trim whitespace."),
    "collapse_ws": (lambda v, a: re.sub(r"\s+", " ", str(v)).strip(), "Collapse runs of whitespace."),
    "upper": (lambda v, a: str(v).upper(), "Upper-case."),
    "lower": (lambda v, a: str(v).lower(), "Lower-case."),
    "title": (lambda v, a: str(v).title(), "Title-case."),
    "digits_only": (lambda v, a: re.sub(r"\D", "", str(v)) or None, "Keep digits only."),
    "zfill": (lambda v, a: str(v).zfill(int(a["width"])), "Left-pad with zeros to args.width (restores leading zeros a spreadsheet dropped)."),
    "length_in": (lambda v, a: v if len(str(v)) in a["lengths"] else None,
                  "Keep the value only if its length is in args.lengths; otherwise no value (not an error). "
                  "Use to route a mixed column, e.g. ABN-or-ACN, into two mappings."),
    "null_if": (_t_null_if, "Treat listed placeholder values as missing. args: values (list)."),
    "date": (_t_date, "Parse to ISO date. args: formats (list of strptime formats, 'iso', 'excel_serial')."),
    "datetime": (lambda v, a: _t_date(v, a, True), "Parse to ISO datetime. Same args as date."),
    "map_values": (_t_map, "Map raw values to enum values. args: map (dict), default (optional), case_insensitive."),
    "abn": (_t_abn, "Digits only, 11 long, ABN checksum. Fails on bad checksum."),
    "acn": (_t_acn, "Digits only, 9 long, ACN checksum. args: from_abn (bool) to take the last 9 digits of a valid ABN."),
    "nzbn": (_t_nzbn, "13-digit NZBN."),
    "state": (_t_state, "Normalise AU state names/abbreviations to the ontology enum."),
    "postcode": (_t_postcode, "4-digit AU postcode, restoring a dropped leading zero."),
    "entity_type_from_name": (_t_entity_type_from_name, "company / incorporated_association from unambiguous legal suffixes, else unknown."),
    "regex_extract": (_t_regex, "args: pattern, group (default 0), required (bool)."),
    "split": (_t_split, "Fan out one cell into several values. args: sep or pattern. Emits one observation each."),
    "url": (_t_url, "Normalise a website."),
}
# Transforms that only clean up the stated value keep it at derivation L0.
L0_TRANSFORMS = {"strip", "collapse_ws", "null_if", "split"}
# The arguments each transform reads. An argument not listed here is silently ignored at run time,
# so the validator rejects it (a common model mistake is nesting them under "args").
TRANSFORM_ARGS = {
    "zfill": {"width"}, "length_in": {"lengths"}, "null_if": {"values"}, "date": {"formats"},
    "datetime": {"formats"}, "map_values": {"map", "default", "case_insensitive"}, "acn": {"from_abn"},
    "regex_extract": {"pattern", "group", "required"}, "split": {"sep", "pattern"},
}
TRANSFORM_REQUIRED_ARGS = {"zfill": {"width"}, "length_in": {"lengths"}, "map_values": {"map"},
                           "regex_extract": {"pattern"}}


def transform_docs():
    return {k: v[1] for k, v in TRANSFORMS.items()}


def apply_transforms(value, transforms):
    """Run one cell through a mapping's transform chain, left to right. Returns a list of values:
    usually one, none if a transform returned None, several after `split`."""
    # The value travels as a list from the start, so `split` fanning one cell out into several
    # values needs no special case: every later transform simply runs on each of them.
    vals = [value]
    for t in transforms or []:
        # A transform is written either as a bare name ("strip") or as {"op": name, ...arguments}.
        op = t if isinstance(t, str) else t.get("op")
        args = {} if isinstance(t, str) else {k: v for k, v in t.items() if k != "op"}
        if op not in TRANSFORMS:
            raise TransformError(f"unknown transform {op!r}")
        fn = TRANSFORMS[op][0]
        out = []
        for v in vals:
            if v is None:
                continue
            r = fn(v, args)
            if isinstance(r, list):
                out.extend(r)
            elif r is not None and str(r) != "":
                out.append(r)
        vals = out
    return vals


# --------------------------------------------------------------------------
# Config -> observations
# --------------------------------------------------------------------------


def config_hash(cfg):
    """A short fingerprint of everything in a config that affects output. The review and agent_run
    blocks are left out: they describe the config, they do not change what it produces. Two uses:
    approval stores this hash, so a later edit is detectable (review.is_approved), and every
    observation is stamped with it, so each value can be traced to the exact config that made it."""
    body = {k: v for k, v in cfg.items() if k not in ("review", "agent_run")}
    return hashlib.sha1(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:12]


def extractor_version(cfg):
    return f"engine-{ENGINE_VERSION}+config-{config_hash(cfg)}"


def _source_value(rec, src):
    # A mapping's source is one column name, or a list of columns joined with ", " (for example
    # street + suburb -> address.full). Empty parts are left out of the join.
    if isinstance(src, list):
        parts = [rec.get(s, "").strip() for s in src]
        return ", ".join(p for p in parts if p)
    return rec.get(src, "")


def record_id(rec, cfg, seen):
    """The source_record_id: how an observation is traced back to its raw row. `seen` counts how
    often each id has come up so far in this run, and the caller must pass the same dict for every
    row of a file."""
    rid_cfg = cfg.get("record_id") or {}
    if rid_cfg.get("strategy") == "fields" and rid_cfg.get("fields"):
        base = "|".join(rec.get(f, "").strip() for f in rid_cfg["fields"])
    else:
        # Deterministic: sha1 of the raw record with sorted keys. Identical
        # duplicate rows get an occurrence suffix (#2, #3) in file order.
        base = "h:" + hashlib.sha1(json.dumps(rec, sort_keys=True).encode()).hexdigest()[:16]
    seen[base] = seen.get(base, 0) + 1
    return base if seen[base] == 1 else f"{base}#{seen[base]}"


def contract_violation(canonical, value):
    """The ontology is a contract: enum and pattern fields are enforced here."""
    spec = canonical_fields().get(canonical)
    if spec is None:
        return f"{canonical} is not an ontology field"
    if spec.get("type") == "enum" and value not in spec["values"]:
        return f"{value!r} is not in {canonical} enum {spec['values']}"
    if spec.get("pattern") and not re.fullmatch(spec["pattern"], str(value)):
        return f"{value!r} does not match {canonical} pattern {spec['pattern']}"
    if spec.get("type") == "date":
        try:
            dt.date.fromisoformat(str(value)[:10])
        except ValueError:
            return f"{value!r} is not an ISO date"
    return None


def observations_for(rec, cfg, rid, ingested_at, errors, xv=None):
    """All canonical observations for one raw record. Pass xv when calling in a loop:
    hashing the config once per record dominated the run time on large sources."""
    xv = xv or extractor_version(cfg)
    meta = cfg.get("source") or {}
    # Envelope values shared by every observation of this record. observed_at starts as the
    # resource's last-modified date and is replaced below if a column is mapped to it.
    observed_at = meta.get("observed_at_default")
    valid = {}
    rows = []
    # Pass 1: run every mapping. Each produces zero or more values for one canonical field.
    for m in cfg.get("mappings") or []:
        canonical = m["canonical"]
        # A mapping either reads column(s) from the record or states a constant (e.g. country = AU).
        raw = m.get("constant") if "constant" in m else _source_value(rec, m["source"])
        try:
            vals = apply_transforms(raw, m.get("transforms"))
        except TransformError as e:
            errors.append({"canonical": canonical, "source": m.get("source"), "raw": raw, "error": str(e)})
            continue
        except Exception as e:  # e.g. a bad regex group that only some values reach. One cell must not kill the run.
            errors.append({"canonical": canonical, "source": m.get("source"), "raw": raw,
                           "error": f"{type(e).__name__}: {e}"})
            continue
        # The observation.* targets are not observations themselves. They set the envelope
        # (observed_at, valid_from, valid_to) that every observation of this record carries.
        if canonical == "observation.observed_at":
            if vals:
                observed_at = str(vals[0])
            continue
        if canonical in ("observation.valid_from", "observation.valid_to"):
            if vals:
                valid[canonical.split(".")[1]] = str(vals[0])
            continue
        # Derivation level: L0 if the value is what the source stated, only tidied (`ops <= ...`
        # means "every transform used is in the tidy-only set"). L1 if it was parsed, normalised,
        # mapped or is a constant, i.e. computed by this engine and not stated by the source.
        ops = {t if isinstance(t, str) else t.get("op") for t in m.get("transforms") or []}
        level = "L0" if ops <= L0_TRANSFORMS and "constant" not in m else "L1"
        for v in vals:
            # Last gate before a value is emitted: it must satisfy the ontology (enum, pattern, date).
            bad = contract_violation(canonical, v)
            if bad:
                errors.append({"canonical": canonical, "source": m.get("source"), "raw": raw, "error": bad})
                continue
            rows.append({
                "field": canonical,
                "value": v,
                "raw_value": raw,
                "source_field": m.get("source"),
                "transforms": m.get("transforms") or [],
                "derivation_level": level,
                "field_confidence": m.get("confidence"),
            })
    # Pass 2: wrap each value in the full envelope. It is a second pass because observed_at and the
    # validity dates may be set by a mapping that comes after the value's own mapping in the config.
    out = []
    for r in rows:
        out.append({
            "source_id": cfg["source_id"],
            "source_record_id": rid,
            "observed_at": observed_at,
            "ingested_at": ingested_at,
            "valid_from": valid.get("valid_from"),
            "valid_to": valid.get("valid_to"),
            "licence": meta.get("licence") or "unknown",
            "extractor_version": xv,
            **r,
            "source_reliability": (cfg.get("source_reliability") or {}).get("value"),
        })
    return out


def iter_records(cfg, path, errors, limit=None, offset=0):
    """Yield (source_record_id, observations) per raw record, streaming."""
    # One ingested_at and one extractor version for the whole run, computed once.
    ingested_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    xv = extractor_version(cfg)
    seen = {}  # record-id counter shared across the file, for the #2, #3 duplicate suffixes
    n = 0
    for i, rec in enumerate(read_records(path, cfg["reader"])):
        if i < offset:
            # Record ids must still see the skipped rows for #n suffixes to be stable.
            record_id(rec, cfg, seen)
            continue
        if limit and n >= limit:
            break
        rid = record_id(rec, cfg, seen)
        yield rid, observations_for(rec, cfg, rid, ingested_at, errors, xv)
        n += 1


def run(cfg, path, limit=None, offset=0):
    """Run a config over a resource. Returns (observations, stats)."""
    obs, errors, n = [], [], 0
    for _, rec_obs in iter_records(cfg, path, errors, limit=limit, offset=offset):
        obs.extend(rec_obs)
        n += 1
    return obs, {"records": n, "observations": len(obs), "errors": errors}
