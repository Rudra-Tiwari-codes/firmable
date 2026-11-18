"""Check configs against their whole file. No model calls, so it costs nothing and can be rerun.

The agent's validation covers a sample. This asks the questions a sample cannot:
  - does each mapping still hold on rows beyond the part of the file the agent worked from?
  - how many non-empty cells silently produce no value?
  - do identifiers and names agree across the whole source?
  - are record ids unique over the whole file, as the ontology needs?
  - what do the licence terms say, and has the source changed since it was approved?

Rows past the first 5,000 are the honest test for the six committed configs: their agent runs read
only the top of each file. For a source with 5,000 rows or fewer there are no such rows, and the
report says so instead of implying a test that did not happen.
"""

import datetime as dt
import json

from . import CONFIGS, OUTPUTS, engine
from .review import is_approved, load_config, raw_status
from .validate import MAX_ERROR_RATE, empirical, semantic, silent_drop_warnings

AUDIT_DIR = OUTPUTS / "audit"
AGENT_WINDOW = 5000  # rows the committed agent runs loaded (LOAD_LIMIT at the time)


def _worst(emp):
    return max((p["error_rate"] for p in emp["per_mapping"]), default=0)


def _strip(emp):
    return [{k: p[k] for k in ("canonical", "source", "raw_non_empty", "records_with_value", "errors", "error_rate",
                               "silent_drop_rate", "error_examples")} for p in emp["per_mapping"]]


def audit_config(slug):
    # Reuses the validator's own empirical and semantic checks, pointed at every row of the file
    # instead of the agent's 500-row holdout. Nothing here calls the model or changes a config.
    cfg = load_config(slug)
    path, raw = raw_status(cfg)
    records = list(engine.read_records(path, cfg["reader"]))
    # Rows the committed agent runs never loaded: the part of the file their configs were not fitted to.
    beyond = records[AGENT_WINDOW:]
    whole = empirical(cfg, records, preview_n=0)
    after = empirical(cfg, beyond, preview_n=0) if beyond else None
    sem = semantic(cfg, records)
    src = cfg["source"]
    flag = engine.licence_flag(src.get("licence_id"))
    return {
        "slug": slug,
        "source_id": cfg["source_id"],
        "approved": is_approved(cfg),
        "reviewer": (cfg.get("review") or {}).get("reviewer"),
        "publisher": src.get("publisher"),
        "resource_url": src.get("resource_url"),
        "licence": src.get("licence"),
        "licence_id": src.get("licence_id"),
        "licence_flag": flag,
        **raw,
        "records_in_file": len(records),
        "share_of_file_agent_window_covers": round(min(AGENT_WINDOW, len(records)) / max(len(records), 1), 3),
        "rows_beyond_agent_window": len(beyond),
        "whole_file": {"worst_error_rate": _worst(whole), "per_mapping": _strip(whole),
                       "record_id_duplicates": whole["record_id_duplicates"]},
        "beyond_agent_window": None if after is None else {
            "worst_error_rate": _worst(after), "per_mapping": _strip(after),
            "mappings_over_error_limit": [p["canonical"] for p in after["per_mapping"]
                                          if p["error_rate"] > MAX_ERROR_RATE]},
        "silent_drop_warnings": silent_drop_warnings(whole),
        "semantic": sem,
    }


def _md(results):
    L = ["# Config audit", "",
         f"Generated {dt.date.today().isoformat()} by `python -m firmable audit`: every config over its whole "
         "file, no model calls. \"Beyond window\" is rows past the first 5,000, which the agent runs behind these "
         "configs never read.", "",
         "| Source | Rows | Agent window covers | Worst error rate (whole file) | Worst error rate (beyond window) | "
         "ABNs with unrelated names | Names under several ABNs | Licence flag | Source changed |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        s = r["semantic"]
        after = r["beyond_agent_window"]
        L.append(f"| {r['slug']} | {r['records_in_file']:,} | {r['share_of_file_agent_window_covers']:.1%} | "
                 f"{r['whole_file']['worst_error_rate']} | {after['worst_error_rate'] if after else 'no such rows'} | "
                 + (f"{s['identifiers_with_unrelated_names']:,} of {s['identifiers']:,} | "
                    f"{s['names_under_several_identifiers']:,} of {s['names']:,}" if s.get("checked") else "not checked | not checked")
                 + f" | {r['licence_flag'] or 'none'} | {'YES' if r['source_drifted'] else 'no'} |")
    L += ["", "## Warnings", ""]
    for r in results:
        msgs = r["silent_drop_warnings"] + (r["semantic"].get("warnings") or [])
        over = (r["beyond_agent_window"] or {}).get("mappings_over_error_limit") or []
        if over:
            msgs.append(f"Over the {MAX_ERROR_RATE:.0%} error limit on rows the agent never read: {', '.join(over)}.")
        if r["whole_file"]["record_id_duplicates"]:
            msgs.append(f"{r['whole_file']['record_id_duplicates']:,} rows needed a #n suffix to keep record ids unique.")
        L.append(f"**{r['slug']}**")
        L.append("")
        L += [f"- {m}" for m in msgs] or ["- none"]
        L.append("")
    return "\n".join(L) + "\n"


def run(slugs=None):
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for slug in slugs or sorted(p.stem for p in CONFIGS.glob("*.yaml")):
        print(f"auditing {slug}")
        results.append(audit_config(slug))
    (AUDIT_DIR / "config_audit.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    (AUDIT_DIR / "config_audit.md").write_text(_md(results), encoding="utf-8")
    print(f"wrote {AUDIT_DIR / 'config_audit.md'}")
    return results
