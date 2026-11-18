"""Validate a proposed mapping config against records outside the model's sample.

Three layers:
  structural - does the config make sense at all (fields exist, transforms and
               their arguments known, enum targets legal)?
  empirical  - run it over held-out records and measure, per mapping, how often
               the transforms fail, produce values the ontology rejects, or
               silently produce nothing from a non-empty cell.
  semantic   - does the output look true, not just well-formed? A checksum proves
               an ABN is a real number, not that it belongs to the name beside it,
               so we measure whether identifiers and names agree with each other.
               These are warnings for the reviewer and the model. They do not fail
               a config, because a real source can legitimately show some of this.

The report goes back to the model in the revise step and to the human in the
review packet.
"""

import re
from collections import defaultdict

from .engine import (TRANSFORM_ARGS, TRANSFORM_REQUIRED_ARGS, TRANSFORMS, TransformError, _source_value,
                     apply_transforms, canonical_fields, contract_violation, record_id)
from .names import name_sim, norm_name

# A mapping fails if more than 5% of its non-empty cells raise an error. Real data always has a few
# bad cells (a mistyped ABN), so zero tolerance would fail every source; 5% separates "a few dirty
# rows" from "this transform is wrong for this column". The number is a judgement, not measured.
MAX_ERROR_RATE = 0.05
# A mapping that turns more than this share of non-empty cells into nothing, without an error, is
# surfaced. Without it a model can "pass" by making a failing pattern optional.
SILENT_DROP_WARN = 0.25
UNRELATED_NAME_SIM = 0.5   # two names scoring below this are treated as different businesses
SEMANTIC_MAX_NAMES = 30    # compare at most 30 names per ABN: pairwise comparison grows with the square


def _check_transform(where, t, issues):
    # Checks one transform without running it: the name exists, every argument is one the
    # transform actually reads, required arguments are present, and a regex compiles.
    op = t if isinstance(t, str) else (t or {}).get("op")
    if op not in TRANSFORMS:
        issues.append((where, f"unknown transform {op!r}"))
        return
    if isinstance(t, str):
        args = set()
    else:
        args = set(t) - {"op"}
    unknown = args - TRANSFORM_ARGS.get(op, set())
    if unknown:
        issues.append((where, f"transform {op!r} has unknown argument(s) {sorted(unknown)}; "
                              f"it reads {sorted(TRANSFORM_ARGS.get(op, set())) or 'none'} and args must be flat, "
                              f"next to 'op'"))
    missing = TRANSFORM_REQUIRED_ARGS.get(op, set()) - args
    if missing:
        issues.append((where, f"transform {op!r} is missing required argument(s) {sorted(missing)}"))
    if op == "regex_extract" and "pattern" in args:
        try:
            compiled = re.compile(t["pattern"])
            if int(t.get("group", 0)) > compiled.groups:
                issues.append((where, f"regex group {t.get('group')} does not exist in {t['pattern']!r}"))
        except re.error as e:
            issues.append((where, f"invalid regex {t['pattern']!r}: {e}"))


def structural(cfg, columns):
    """Layer 1: is the config well-formed? Checked against the ontology and the file's real column
    names, without running any data. This catches a model inventing a column, a canonical field or
    a transform. Returns a list of {where, problem}; an empty list means it is sound."""
    issues = []
    fields = canonical_fields()
    cols = set(columns)
    for i, m in enumerate(cfg.get("mappings") or []):
        where = f"mappings[{i}] {m.get('canonical')}"
        if m.get("canonical") not in fields:
            issues.append((where, f"unknown canonical field {m.get('canonical')!r}"))
        if "constant" not in m:
            srcs = m.get("source")
            srcs = srcs if isinstance(srcs, list) else [srcs]
            missing = [s for s in srcs if s not in cols]
            if missing:
                issues.append((where, f"source column(s) not in data: {missing}"))
        for t in m.get("transforms") or []:
            _check_transform(where, t, issues)
        c = m.get("confidence")
        if not isinstance(c, (int, float)) or not 0 <= c <= 1:
            issues.append((where, f"confidence must be a number in [0,1], got {c!r}"))
    rid = cfg.get("record_id") or {}
    if rid.get("strategy") == "fields":
        missing = [f for f in rid.get("fields") or [] if f not in cols]
        if missing or not rid.get("fields"):
            issues.append(("record_id", f"key fields missing from data: {missing or 'none given'}"))
    elif rid.get("strategy") != "row_hash":
        issues.append(("record_id", "strategy must be 'fields' or 'row_hash'"))
    if (cfg.get("source_reliability") or {}).get("value") is None:
        issues.append(("source_reliability", "missing value"))
    return [{"where": w, "problem": p} for w, p in issues]


def empirical(cfg, holdout, preview_n=8):
    """Layer 2: run every mapping over the held-out rows and count what happens. Each non-empty
    cell ends one of three ways: it produced a value, it raised an error, or it silently produced
    nothing. The three rates per mapping are the heart of the validation report."""
    per_mapping = []
    for i, m in enumerate(cfg.get("mappings") or []):
        raw_nonempty = produced = 0
        errs = []
        for rec in holdout:
            raw = m.get("constant") if "constant" in m else _source_value(rec, m.get("source"))
            if str(raw or "").strip():
                raw_nonempty += 1
            try:
                vals = apply_transforms(raw, m.get("transforms"))
            except TransformError as e:
                errs.append({"raw": raw, "error": str(e)})
                continue
            except Exception as e:  # a bad arg in a transform is the config's fault, report it
                errs.append({"raw": raw, "error": f"{type(e).__name__}: {e}"})
                continue
            if m.get("canonical", "").startswith("observation."):
                produced += bool(vals)
                continue
            ok = [v for v in vals if not contract_violation(m["canonical"], v)]
            for v in vals:
                bad = contract_violation(m["canonical"], v)
                if bad:
                    errs.append({"raw": raw, "error": bad})
            produced += bool(ok)
        # Rates are out of the non-empty cells, so a column that is mostly blank is not punished
        # for being blank. max(..., 1) avoids dividing by zero on an all-empty column.
        # silent drops = non-empty cells that gave neither a value nor an error.
        denom = max(raw_nonempty, 1)
        per_mapping.append({
            "index": i,
            "canonical": m.get("canonical"),
            "source": m.get("source"),
            "raw_non_empty": raw_nonempty,
            "records_with_value": produced,
            "errors": len(errs),
            "error_rate": round(len(errs) / denom, 3),
            "silent_drop_rate": round(max(raw_nonempty - produced - len(errs), 0) / denom, 3),
            "error_examples": _distinct(errs, 5),
        })

    # Is the chosen record key unique? record_id() adds "#2", "#3" to a repeated id, so counting
    # ids that contain "#" counts the repeats.
    seen, base_dupes = {}, 0
    for rec in holdout:
        rid = record_id(rec, cfg, seen)
        if "#" in rid:
            base_dupes += 1

    # A few records shown as raw value -> canonical value, for the model and the human to read.
    preview = []
    for rec in holdout[:preview_n]:
        out = {}
        for m in cfg.get("mappings") or []:
            raw = m.get("constant") if "constant" in m else _source_value(rec, m.get("source"))
            try:
                out[m["canonical"]] = {"raw": raw, "values": apply_transforms(raw, m.get("transforms"))}
            except Exception as e:
                out[m["canonical"]] = {"raw": raw, "error": str(e)}
        preview.append(out)

    return {
        "holdout_records": len(holdout),
        "per_mapping": per_mapping,
        "record_id_duplicates": base_dupes,
        "preview": preview,
    }


def _distinct(errs, n):
    # Up to n error examples with different messages, so the model sees 5 kinds of failure and not
    # the same failure 5 times.
    out, seen = [], set()
    for e in errs:
        k = e["error"][:80]
        if k not in seen:
            seen.add(k)
            out.append({"raw": str(e["raw"])[:100], "error": e["error"][:200]})
        if len(out) == n:
            break
    return out


def _first_values(rec, mappings):
    vals = []
    for m in mappings:
        try:
            vals += apply_transforms(_source_value(rec, m.get("source")), m.get("transforms"))
        except Exception:
            continue
    return [str(v) for v in vals]


def _unrelated(names):
    """True if any two of these normalised names look like different businesses."""
    ns = sorted(names)[:SEMANTIC_MAX_NAMES]
    for i, a in enumerate(ns):
        for b in ns[i + 1:]:
            if (name_sim(a, b) or 0) < UNRELATED_NAME_SIM:
                return True
    return False


def semantic(cfg, records, examples=5):
    """Do identifiers and names agree? One ABN carrying unrelated names, or one name spread over
    many ABNs, means the checksum-valid identifier may not belong to the name in that row."""
    maps = [m for m in cfg.get("mappings") or [] if "constant" not in m]
    ids = [m for m in maps if m.get("canonical") == "entity.abn"]
    names = [m for m in maps if m.get("canonical") == "entity.legal_name"]
    if not ids or not names:
        return {"checked": False, "reason": "needs both entity.abn and entity.legal_name mapped", "warnings": []}
    # Two lookups built in one pass: ABN -> every name seen beside it, and name -> every ABN seen
    # beside it. In a clean register both are close to one-to-one.
    id_names, name_ids = defaultdict(set), defaultdict(set)
    for rec in records:
        abns = _first_values(rec, ids)
        norms = {norm_name(n) for n in _first_values(rec, names)} - {""}
        for abn in abns:
            id_names[abn] |= norms
            for n in norms:
                name_ids[n].add(abn)
    multi = {a: ns for a, ns in id_names.items() if len(ns) > 1}
    unrelated = {a: ns for a, ns in multi.items() if _unrelated(ns)}
    spread = {n: ids_ for n, ids_ in name_ids.items() if len(ids_) > 1}
    out = {
        "checked": True,
        "records_checked": len(records),
        "identifiers": len(id_names),
        "identifiers_with_several_names": len(multi),
        "identifiers_with_unrelated_names": len(unrelated),
        "names": len(name_ids),
        "names_under_several_identifiers": len(spread),
        "examples_unrelated": [{"abn": a, "names": sorted(ns)[:6]} for a, ns in
                               sorted(unrelated.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:examples]],
        "examples_spread": [{"name": n, "identifiers": len(i)} for n, i in
                            sorted(spread.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:examples]],
    }
    # Warn only above 2% of identifiers or names. A handful of such cases is normal (a business
    # renamed, a franchise name); a large share means the name/ABN pairing itself is unreliable.
    warnings = []
    if id_names and len(unrelated) / len(id_names) > 0.02:
        warnings.append(f"{len(unrelated)} of {len(id_names)} ABNs carry names that look unrelated. A valid checksum "
                        f"does not prove the ABN belongs to the name in that row. Either the pairing is unreliable, or "
                        f"several parties legitimately share an ABN (e.g. employees licensed under an employer). Check which.")
    if name_ids and len(spread) / len(name_ids) > 0.02:
        warnings.append(f"{len(spread)} of {len(name_ids)} names appear under more than one ABN.")
    out["warnings"] = warnings
    return out


def silent_drop_warnings(emp):
    out = []
    for p in (emp or {}).get("per_mapping", []):
        if p.get("silent_drop_rate", 0) > SILENT_DROP_WARN:
            out.append(f"{p['canonical']} (from {p['source']}): {p['silent_drop_rate']:.0%} of non-empty cells "
                       f"produce no value and no error. Confirm that is intended filtering.")
    return out


def validate(cfg, columns, holdout, semantic_records=None):
    """Run all three layers and return one report. `passed` is true only if there are no
    structural issues and no failing mapping. Warnings never change `passed`."""
    s = structural(cfg, columns)
    # A structurally broken config is not run at all: the model has to fix its shape first.
    e = empirical(cfg, holdout) if not s else None
    failing = []
    if e and not holdout:
        failing = [{"canonical": "(all)", "error": "no held-out records: this config has not been tested on anything"}]
    elif e:
        failing = [p for p in e["per_mapping"] if p["error_rate"] > MAX_ERROR_RATE]
        if e["record_id_duplicates"] and (cfg.get("record_id") or {}).get("strategy") == "fields":
            failing.append({"canonical": "record_id",
                            "error": f"{e['record_id_duplicates']} duplicate keys in holdout"})
    sem = semantic(cfg, semantic_records if semantic_records is not None else holdout) if not s else None
    warnings = silent_drop_warnings(e) + ((sem or {}).get("warnings") or [])
    return {
        "passed": not s and not failing,
        "structural_issues": s,
        "failing": failing,
        "warnings": warnings,
        "semantic": sem,
        "empirical": e,
        "max_error_rate": MAX_ERROR_RATE,
    }
