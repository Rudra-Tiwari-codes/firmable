"""Part 2: the onboarding agent.

Given only a CKAN dataset id, it produces a mapping config for the generic
engine and stops at a human approval gate. Nobody writes the config by hand.

Steps (state is saved after each one, so a rerun resumes at the first step
that has not completed):

  1. select_resource  LLM   pick the resource most likely to hold entity-level rows
  2. download         det   fetch it; on failure fall back to the model's runner-up picks
  3. inspect          LLM   decide what the file really is and how to read it
  4. load             det   parse with that reader; on failure the model repairs the
                            reader config from the error (up to 3 tries)
  5. profile          det   draw a random 5,000 rows from the whole file and split them: a 25-row
                            sample (model reads), a holdout (revise loop), a final test (model
                            never sees it). Column stats exclude holdout and final test.
  6. propose          LLM   draft mapping config against the ontology
  7. validate_revise  det+LLM  run the config over the holdout, hand the report back,
                            model revises; repeat until it passes and the model signs off,
                            max 3 rounds. Then score the final test once, with no model call.
  8. finalize         det   write configs/<slug>.yaml (status: pending) and a review packet

The human gate is `python -m firmable review <slug>`.
"""

import datetime as dt
import json
import time
import traceback

import yaml

from . import CONFIGS, DATA, OUTPUTS, ROOT, engine, llm
from .ckan import CKAN
from .inspect_tools import (INSPECT_SEEN_ROWS, describe_bytes, download, profile_columns, reservoir_sample,
                            split_records)
from .validate import validate

RUNS = DATA / "runs"   # one folder per source: state.json plus one JSON file per step's result
# The steps in running order. onboard() walks this list, and each name is looked up in STEP_FNS.
STEPS = ["select_resource", "download", "inspect", "load", "profile", "propose", "validate_revise", "finalize"]
LOAD_LIMIT = 5000      # rows drawn from the file for the agent to work on (sample + holdout + final test + profile)
MAX_ROUNDS = 3         # most revise calls per source: this bounds both the cost and the loop


# --------------------------------------------------------------------------
# Prompts. SYSTEM is identical on every call so it can be cached.
#
# SYSTEM carries what never changes between sources: the rules, the ontology file (read from disk
# and pasted in), the transform vocabulary (generated from engine.TRANSFORMS, so the prompt cannot
# drift from the code) and the shape a config must have. Each step then sends its own user message
# with that step's evidence. In this f-string {{ and }} are literal braces; { } inserts a value.
# --------------------------------------------------------------------------

SYSTEM = f"""You are the schema-inference agent in a data onboarding pipeline for Firmable, which keeps a
resolved graph of Australian and New Zealand businesses. You turn an unseen public dataset into a
mapping config that a generic extractor runs. A human approves your config before it is used.

Principles, in priority order:
- A wrong mapping is worse than no mapping. If you are not confident a column means a canonical field,
  leave it unmapped and say why. Never guess entity_type or status; the ontology default is unknown.
- legal_name is the registered name. trading_name is a business or trading name. A column like
  "Licensee" or "Supplier" is a legal_name only if the data shows registered names (Pty Ltd etc.);
  otherwise it is ambiguous, so say so and lower the confidence.
- Confidence on a mapping means "how sure are we that this column, after these transforms, is this
  canonical field". It is not source reliability and not entity-link confidence.
- Use only the transform vocabulary below. You cannot write code.
- Reply with one JSON object only, no prose outside it.

The canonical ontology (the contract; canonical names are "<section>.<field>", e.g. entity.abn,
address.postcode, observation.observed_at, observation.valid_from, observation.valid_to):

{(ROOT / "firmable_ontology.yaml").read_text(encoding="utf-8")}

Transform vocabulary (each transform is {{"op": name, ...args}}; they run left to right; a transform
that fails marks the value as a parse error and it is dropped, never emitted):
{json.dumps(engine.transform_docs(), indent=2)}

Mapping config shape:
{{
  "record_id": {{"strategy": "fields", "fields": ["col", ...], "rationale": "..."}}
               or {{"strategy": "row_hash", "rationale": "no stable key in the source"}},
  "mappings": [
    {{"canonical": "entity.legal_name", "source": "Column name" or ["Col A", "Col B"] (joined with ", "),
      "transforms": [{{"op": "collapse_ws"}}],
      "confidence": 0.0-1.0, "rationale": "one line"}},
    {{"canonical": "address.country", "constant": "AU", "confidence": 0.9, "rationale": "..."}}
  ],
  "unmapped_source_fields": [{{"source": "col", "reason": "..."}}],
  "unmapped_canonical_fields": [{{"canonical": "entity.acn", "reason": "not present"}}],
  "relationships": [{{"source": "col", "kind": "parent|trustee|agent|other", "note": "..."}}],
  "source_reliability": {{"value": 0.0-1.0, "rationale": "trust in this publisher for these claims"}},
  "notes": ["anything a reviewer should know"]
}}
Map observation.observed_at only to a per-record column saying when the source stated the claim; if
there is none the engine uses the resource's last-modified date. Map observation.valid_from /
valid_to only to columns that say when the claim became true / stopped being true (e.g. licence
start / expiry, registration / deregistration dates).
"""


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _trim(rec, n=120):
    # Cut very long cell values before showing a record to the model: saves tokens, loses no meaning.
    return {k: (v[:n] + "…" if isinstance(v, str) and len(v) > n else v) for k, v in rec.items()}


# --------------------------------------------------------------------------
# Run state
# --------------------------------------------------------------------------


class Run:
    """The agent's memory, kept on disk in data/runs/<slug>/. This is the "state carried between
    steps": a step reads what earlier steps saved with get() and saves its own result with put().

      state.json   which steps are done or failed, timings, and the ledger of every model call
      <name>.json  one file per result (select_resource, inspect, profile, config_v1, rounds, ...)

    Because everything is on disk, a crashed or interrupted run loses nothing: a new Run(slug)
    loads the same state and onboard() carries on from the first step that is not done."""

    def __init__(self, slug):
        self.slug = slug
        self.dir = RUNS / slug
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "state.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
            "slug": slug, "created_at": _now(), "steps": {}, "llm_calls": []}

    def save(self):
        self.path.write_text(json.dumps(self.state, indent=2, default=str), encoding="utf-8")

    def put(self, name, obj):
        (self.dir / f"{name}.json").write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")

    def get(self, name):
        p = self.dir / f"{name}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def done(self, step):
        return self.state["steps"].get(step, {}).get("status") == "done"

    def ask(self, step, user, effort="medium"):
        """The only place the agent calls the model. Every call's token usage and cost is written
        to the ledger in state.json and saved straight away, including calls that failed."""
        try:
            obj, usage = llm.call_json(step, SYSTEM, user, effort=effort)
        except llm.LLMError as e:
            if e.usage:  # the call failed but was billed; the ledger still has to show it
                e.usage.update(step=step, at=_now(), failed=str(e))
                self.state["llm_calls"].append(e.usage)
                self.save()
            raise
        usage.update(step=step, at=_now())
        self.state["llm_calls"].append(usage)
        self.save()
        print(f"    [{step}] {usage['input_tokens']}+{usage['cache_read_input_tokens']}c in / "
              f"{usage['output_tokens']} out, ${usage['usd']}, {usage['seconds']}s")
        return obj


def slug_for(pkg):
    # The slug names the run folder, the config file and the review packet. It is the dataset's
    # CKAN name, cut at 60 characters to keep file paths short.
    return (pkg.get("name") or pkg["id"])[:60]


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


def step_select_resource(run, pkg):
    # Step 1 (model). A CKAN dataset is a bundle of files ("resources"): the data in several
    # formats, PDFs, data dictionaries. The model sees only their metadata and picks one, plus
    # fallbacks in case its pick will not download.
    resources = [{
        "id": r["id"], "name": r.get("name"), "format": r.get("format"), "size": r.get("size"),
        "datastore_active": r.get("datastore_active"), "last_modified": r.get("last_modified"),
        "url": r.get("url"), "description": (r.get("description") or "")[:200],
    } for r in pkg.get("resources") or []]
    user = f"""Step: select_resource.
Dataset: {json.dumps(_meta(pkg), indent=2)}
Resources: {json.dumps(resources, indent=2)}

Pick the single resource most likely to contain one row per business-like entity (or per event
naming one), in a machine-readable form we can download (prefer under ~100 MB; avoid PDFs, HTML
landing pages, data dictionaries and aggregate summaries). Also give up to 2 fallbacks in order.
Reply: {{"resource_id": "...", "fallback_ids": ["..."], "reason": "one line"}}"""
    out = run.ask("select_resource", user, effort="low")
    # The model's answer is checked against the real list before it is trusted: an id that does
    # not exist stops the step, and made-up fallback ids are dropped.
    ids = {r["id"] for r in resources}
    if out.get("resource_id") not in ids:
        raise ValueError(f"model picked a resource id that does not exist: {out.get('resource_id')}")
    out["fallback_ids"] = [i for i in out.get("fallback_ids") or [] if i in ids]
    run.put("select_resource", out)


def step_download(run, pkg):
    # Step 2 (no model). Try the model's pick, then its fallbacks, and keep the first usable file.
    # A candidate is rejected if the download fails, the file is over the size cap, or its bytes
    # turn out to be a web page, a PDF or a broken zip. Every rejection is recorded in `tried`.
    sel = run.get("select_resource")
    tried = []
    for rid in [sel["resource_id"]] + sel["fallback_ids"]:
        res = next(r for r in pkg["resources"] if r["id"] == rid)
        try:
            path, meta = download(res["url"], rid)
        except Exception as e:
            tried.append({"resource_id": rid, "error": str(e)})
            continue
        if meta.get("truncated"):  # part of a file would parse cleanly and quietly lose the rest
            tried.append({"resource_id": rid, "error": "file is larger than the download cap"})
            continue
        sniff = describe_bytes(path)
        if sniff["sniffed"].startswith(("html", "pdf", "corrupt")):
            tried.append({"resource_id": rid, "error": f"unusable: {sniff['sniffed']}"})
            continue
        run.put("download", {"resource": res, "path": str(path), "meta": meta, "tried": tried})
        return
    raise RuntimeError(f"no usable resource: {tried}")


def step_inspect(run, pkg):
    # Step 3 (model). The model is shown what the bytes look like (first lines, sheet previews, JSON
    # skeleton or XML tag counts) and answers two things: what this file is, and the reader config
    # the engine needs to parse it. Nothing is mapped yet; this step is only about reading.
    dl = run.get("download")
    sniff = describe_bytes(dl["path"])
    run.put("raw_description", sniff)
    user = f"""Step: inspect.
Dataset: {json.dumps(_meta(pkg), indent=2)}
Resource: {json.dumps({k: dl['resource'].get(k) for k in ('name', 'format', 'url', 'description')}, indent=2)}
Download: {json.dumps(dl['meta'], indent=2)}
What the bytes look like (sniffed independently of the declared format):
{json.dumps(sniff, indent=2, default=str)[:12000]}

Work out what this file actually is and how to read it into one record per row/element.
Reader options: format (csv|tsv|xlsx|json|jsonl|xml|zip), encoding, delimiter, header_row (0-based
number of lines/rows to skip before the header), sheet (xlsx: a sheet name, or "*" when every sheet
holds the same table, e.g. one sheet per year; rows then carry a _sheet column you can use in
record_id), records_path (json, dotted path to the list of records), record_tag (xml element name
that is one record), zip_member and inner_format (zip). Watch for title rows above the real header,
notes rows, multiple sheets, and files whose declared format is wrong.
Reply: {{"what_it_is": "...", "record_grain": "one record = ...",
 "contains_business_entities": true/false, "evidence": "...",
 "reader": {{...}}, "concerns": ["..."]}}"""
    run.put("inspect", run.ask("inspect", user))


def _load_check(records):
    """A reader can run without an error and still be wrong. These catch the common ways: no
    records at all, mostly blank header cells (wrong header_row), or everything in one column
    (wrong delimiter). Returns a description of the problem, or None if the parse looks sane."""
    if not records:
        return "reader produced 0 records"
    cols = list(records[0].keys())
    generic = sum(c.startswith("column_") for c in cols)
    if generic > len(cols) / 2:
        return f"{generic}/{len(cols)} header cells are blank; header_row is probably wrong. Header: {cols[:15]}"
    if len(cols) == 1 and len(records) > 1:
        return f"only one column ({cols[0][:80]!r}); delimiter or format is probably wrong"
    return None


def step_load(run, pkg):
    # Step 4 (no model unless it fails). Parse the file with the model's reader config. If that
    # raises or looks wrong, the problem goes back to the model, which returns a corrected reader.
    # This is the first self-correction loop. It ends at 3 attempts, and the reader that finally
    # works is the one written into the config.
    dl, insp = run.get("download"), run.get("inspect")
    reader = insp["reader"]
    attempts = []
    for attempt in range(3):
        try:
            records = list(engine.read_records(dl["path"], reader, limit=LOAD_LIMIT))
            problem = _load_check(records)
        except Exception as e:
            records, problem = [], f"{type(e).__name__}: {e}"
        if not problem:
            run.put("load", {"reader": reader, "records_loaded": len(records),
                             "columns": list(records[0].keys()), "attempts": attempts})
            return
        attempts.append({"reader": reader, "problem": problem})
        print(f"    load attempt {attempt + 1} failed: {problem[:160]}")
        if attempt == 2:
            break
        user = f"""Step: repair_reader.
Your reader config failed when the engine ran it.
Previous attempts and the problem each hit: {json.dumps(attempts, indent=2)}
What the bytes look like: {json.dumps(run.get('raw_description'), indent=2, default=str)[:10000]}
Return a corrected reader. Reply: {{"reader": {{...}}, "diagnosis": "..."}}"""
        reader = run.ask("repair_reader", user)["reader"]
    run.put("load_failure", attempts)
    raise RuntimeError(f"could not read resource after 3 attempts: {attempts[-1]['problem']}")


def _records(run):
    """The rows this run works on. Deterministic, so every step sees the same rows.
    Runs made before split_version 2 used the top of the file; resuming one keeps that."""
    dl, ld = run.get("download"), run.get("load")
    prof = run.get("profile")
    if prof and not prof.get("split_version"):
        return list(engine.read_records(dl["path"], ld["reader"], limit=LOAD_LIMIT))
    return sample_rows(dl["path"], ld["reader"])[0]


def sample_rows(path, reader):
    return reservoir_sample(engine.read_records(path, reader), LOAD_LIMIT, skip=INSPECT_SEEN_ROWS)


def step_profile(run, pkg):
    # Step 5 (no model). Draw up to 5,000 random rows from the whole file and split them into the
    # rows the model reads (sample), the rows its config is tested on while it revises (holdout),
    # and rows kept completely away from it (final test). Column statistics are computed only on
    # rows outside the holdout and final test, so they cannot leak test rows into the prompt.
    dl, ld = run.get("download"), run.get("load")
    records, total = sample_rows(dl["path"], ld["reader"])
    split = split_records(records)
    profile_rows = [records[i] for i in split["profile_idx"]]
    run.put("profile", {
        "split_version": 2,
        "records_in_file": total,
        "records_sampled": len(records),
        "records_profiled": len(profile_rows),
        "columns": profile_columns(profile_rows),
        "split_note": "uniform random rows from the whole file; column stats exclude holdout and final test",
        "holdout_idx": split["holdout_idx"],
        "final_test_idx": split["final_test_idx"],
    })
    run.put("sample", [records[i] for i in split["sample_idx"]])


def step_propose(run, pkg):
    # Step 6 (model, high effort). The first draft of the mapping. The model gets its own earlier
    # inspection, the column profiles and the 25 sample records, and replies with a config body:
    # record_id, mappings, what it left unmapped and why, relationships, source_reliability, notes.
    # The draft is saved as config_v1. Each revision later becomes config_v2, v3, ...
    insp, prof, sample = run.get("inspect"), run.get("profile"), run.get("sample")
    user = f"""Step: propose.
Dataset: {json.dumps(_meta(pkg), indent=2)}
Your inspection: {json.dumps({k: insp.get(k) for k in ('what_it_is', 'record_grain', 'evidence', 'concerns')}, indent=2)}
Column profiles over {prof['records_profiled']} records (pattern_rates are the share of non-empty
values matching each pattern, e.g. abn_checksum_valid = passes the real ABN checksum):
{json.dumps(prof['columns'], indent=1)[:30000]}

{len(sample)} sample records:
{json.dumps([_trim(r) for r in sample], indent=1)[:30000]}

Propose the mapping config. It will be run over held-out records outside everything above, and
you will get the results back. Reply with the config object only."""
    cfg = run.ask("propose", user, effort="high")
    _save_version(run, cfg, 1)


def _save_version(run, body, n):
    run.state["config_versions"] = n
    run.save()
    run.put(f"config_v{n}", body)


def _full_config(run, pkg, body):
    """Assemble the complete config from three origins:
      source block   facts copied from the catalogue and the download (never written by the model)
      reader         the reader that worked in the load step
      everything else  the model's body: record_id, mappings, unmapped lists, notes, ...
    The last line refuses to let the model's body overwrite source_id, source or reader."""
    dl = run.get("download")
    res = dl["resource"]
    cfg = {
        "source_id": f"datagovau:{pkg['name']}:{res['id'][:8]}",
        "source": {
            "portal": "data.gov.au",
            "dataset_id": pkg["id"],
            "dataset_name": pkg["name"],
            "title": pkg.get("title"),
            "publisher": (pkg.get("organization") or {}).get("title"),
            "resource_id": res["id"],
            "resource_url": res.get("url"),
            "licence": pkg.get("license_title") or pkg.get("license_id") or "unknown",
            "licence_id": pkg.get("license_id"),
            "licence_flag": engine.licence_flag(pkg.get("license_id")),
            "observed_at_default": res.get("last_modified") or pkg.get("metadata_modified"),
            "raw_sha256": dl["meta"]["sha256"],
        },
        "reader": run.get("load")["reader"],
    }
    cfg.update({k: v for k, v in body.items() if k not in ("source_id", "source", "reader")})
    return cfg


def step_validate_revise(run, pkg):
    # Step 7, the core of the agent: the second self-correction loop.
    #   1. run the current config over the holdout rows and build a report (validate.py)
    #   2. send the config and the report to the model
    #   3. the model either signs off ("no_changes") or returns a full revised config
    #   4. a revised config becomes the next version, and the loop goes back to 1
    # It ends when the model signs off on a passing config or MAX_ROUNDS is used up. Whatever
    # version is current at that point goes to the human, with its report, even if it still fails.
    records = _records(run)
    prof = run.get("profile")
    holdout = [records[i] for i in prof["holdout_idx"]]
    final_idx = prof.get("final_test_idx") or []
    # Identifier/name examples go back to the model, so final-test rows stay out of what it is shown.
    hidden = set(final_idx)
    visible = [r for i, r in enumerate(records) if i not in hidden]
    # Every column seen in any row: a JSON source's first record may not have all of them.
    columns = list(dict.fromkeys(c for r in records for c in r))
    n = run.state.get("config_versions", 1)
    rounds = run.get("rounds") or []
    while True:
        body = run.get(f"config_v{n}")
        cfg = _full_config(run, pkg, body)
        report = validate(cfg, columns, holdout, semantic_records=visible)
        run.put(f"validation_v{n}", report)
        print(f"    v{n}: passed={report['passed']} structural={len(report['structural_issues'])} "
              f"failing={len(report['failing'])} warnings={len(report['warnings'])}")
        # The budget check sits after validation on purpose: the last revision the model makes is
        # still validated before the loop ends, so the final config always has a report.
        if len(rounds) >= MAX_ROUNDS:
            break
        # If a reviewer sent the config back, their feedback is added to every revise prompt.
        fb = run.state.get("human_feedback") or []
        fb_txt = ("\nA human reviewer sent this config back. Their feedback overrides your earlier choices:\n"
                  + "\n".join(f"- {f['text']}" for f in fb) + "\n") if fb else ""
        user = f"""Step: revise (round {len(rounds) + 1} of at most {MAX_ROUNDS}).
{fb_txt}Your current config (v{n}): {json.dumps(body, indent=1)}

The engine ran it over {len(holdout)} held-out records outside your sample. Once it stops revising,
it scores a separate final test set that you never see. Report:
{json.dumps(_compact_report(report), indent=1)[:30000]}

Fix every structural issue and every mapping whose error_rate exceeds {report['max_error_rate']}.
Making a failing pattern optional is not a fix: it only moves the failure into silent_drop_rate.
Then, even if everything passed, check the preview against meaning, not just syntax: is each value
really the canonical field you claimed? Is anything mapped that you should have left unmapped, or
any confidence too high? Read "warnings" and "semantic": if identifiers and names disagree, a
checksum-valid ABN may not belong to the name in that row, so say so in notes and lower the
confidence of the mappings involved. Prefer unmapping over a fragile transform.
Reply: {{"no_changes": true/false, "changes": [{{"what": "...", "why": "..."}}], "config": {{full config}}}}"""
        out = run.ask("revise", user, effort="high")
        rounds.append({"from_version": n, "no_changes": bool(out.get("no_changes")),
                       "changes": out.get("changes") or [], "passed_before": report["passed"]})
        run.put("rounds", rounds)
        if out.get("no_changes") and (report["passed"] or not isinstance(out.get("config"), dict)):
            break  # signed off, or nothing further to try; a failing report still goes to the human as failing
        if not isinstance(out.get("config"), dict):
            raise ValueError("revise reply has changes but no config object")
        n += 1
        _save_version(run, out["config"], n)
    final = {"version": n, "passed": report["passed"], "rounds": len(rounds)}
    # The holdout report was shown to the model, so the config may have been shaped to fit those
    # rows. The final test rows were never shown in any prompt, so this score is the honest one.
    if final_idx:
        # Scored once, with no model call, so this result cannot have shaped the config.
        final_report = validate(cfg, columns, [records[i] for i in final_idx], semantic_records=records)
        run.put("validation_final", final_report)
        final.update(final_test_passed=final_report["passed"], final_test_records=len(final_idx))
        print(f"    final test on {len(final_idx)} records: passed={final_report['passed']} "
              f"failing={len(final_report['failing'])}")
    run.put("final", final)


def _compact_report(report):
    r = dict(report)
    if r.get("empirical"):
        e = dict(r["empirical"])
        e["preview"] = e["preview"][:6]
        r["empirical"] = e
    return r


def step_finalize(run, pkg):
    # Step 8 (no model). Write the final config to configs/<slug>.yaml with two extra blocks:
    # agent_run (what the run cost) and review (status pending). Then write the review packet.
    # The agent stops here. Nothing downstream will use the config until a person approves it.
    fin = run.get("final")
    body = run.get(f"config_v{fin['version']}")
    cfg = _full_config(run, pkg, body)
    report = run.get(f"validation_v{fin['version']}")
    calls = run.state["llm_calls"]
    steps = run.state["steps"]
    cfg["agent_run"] = {
        "engine_version": engine.ENGINE_VERSION,
        "model": llm.MODEL,
        "config_version": fin["version"],
        "revise_rounds": fin["rounds"],
        "validation_passed": fin["passed"],
        "final_test_passed": fin.get("final_test_passed"),
        "llm_calls": len(calls),
        "input_tokens": sum(c["input_tokens"] + c["cache_read_input_tokens"] + c["cache_creation_input_tokens"] for c in calls),
        "output_tokens": sum(c["output_tokens"] for c in calls),
        "usd": round(sum(c["usd"] or 0 for c in calls), 4),
        "wall_seconds": round(sum(s.get("seconds", 0) for s in steps.values()), 1),
        "steps": len(STEPS),
    }
    # A config that failed validation or the final test is still written, but marked
    # pending_needs_attention, and review.py then refuses to approve it without a written reason.
    clean = fin["passed"] and fin.get("final_test_passed") is not False
    cfg["review"] = {"status": "pending" if clean else "pending_needs_attention",
                     "reviewer": None, "at": None, "notes": None}
    CONFIGS.mkdir(exist_ok=True)
    path = CONFIGS / f"{run.slug}.yaml"
    header = (f"# Mapping config generated by the onboarding agent ({llm.MODEL}), not written by hand.\n"
              f"# Review with: python -m firmable review {run.slug}\n")
    path.write_text(header + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
    from .review import write_packet
    write_packet(run.slug, cfg, report, run)
    print(f"  wrote {path.relative_to(ROOT)}")


STEP_FNS = {
    "select_resource": step_select_resource,
    "download": step_download,
    "inspect": step_inspect,
    "load": step_load,
    "profile": step_profile,
    "propose": step_propose,
    "validate_revise": step_validate_revise,
    "finalize": step_finalize,
}


def _meta(pkg):
    return {
        "id": pkg["id"], "name": pkg.get("name"), "title": pkg.get("title"),
        "publisher": (pkg.get("organization") or {}).get("title"),
        "licence": pkg.get("license_title") or pkg.get("license_id"),
        "metadata_modified": pkg.get("metadata_modified"),
        "description": (pkg.get("notes") or "")[:1500],
    }


def human_feedback(slug, text):
    """Reviewer sent the config back: record it, then rerun revise and finalize."""
    run = Run(slug)
    if not run.state.get("dataset"):
        raise SystemExit(f"no agent run found for {slug}; feedback goes to a config the agent wrote")
    run.state.setdefault("human_feedback", []).append({"text": text, "at": _now()})
    run.put("rounds", [])  # fresh revise budget; the feedback stays in every prompt
    run.save()
    return onboard(run.state["dataset"]["id"], restart_from="validate_revise")


def onboard(dataset, restart_from=None, resource=None):
    """The agent's main loop. Takes a CKAN dataset id or name and runs the 8 steps in order.
    Returns the slug on success, or None if a step failed (the state is kept for a resume)."""
    pkg = CKAN().package_show(dataset)
    run = Run(slug_for(pkg))
    run.state["dataset"] = _meta(pkg)
    if resource and not run.done("select_resource"):
        # A human pinned the resource (choosing data, not writing config). Recorded as such.
        if resource not in {r["id"] for r in pkg["resources"]}:
            raise SystemExit(f"resource {resource} is not in dataset {pkg['name']}")
        run.put("select_resource", {"resource_id": resource, "fallback_ids": [], "reason": "pinned by human"})
        run.state["steps"]["select_resource"] = {"status": "done", "pinned_by_human": True, "seconds": 0}
        run.save()
    # --restart-from forgets that step and every step after it, so they all run again.
    if restart_from:
        idx = STEPS.index(restart_from)
        for s in STEPS[idx:]:
            run.state["steps"].pop(s, None)
        if idx <= STEPS.index("propose"):
            run.put("rounds", [])  # a fresh proposal gets a fresh revise budget
    print(f"onboarding {pkg.get('title')!r} -> {run.slug}")
    for name in STEPS:
        # Resume: a step already marked done is skipped, so paid model calls are never repeated.
        if run.done(name):
            print(f"  {name}: done (resumed)")
            continue
        print(f"  {name} ...")
        # Mark the step "running" and save before starting it, then "done" or "failed" after.
        # A failure stops the whole run here; the steps before it stay done.
        t0 = time.time()
        rec = run.state["steps"].setdefault(name, {})
        rec.update(status="running", started_at=_now(), attempts=rec.get("attempts", 0) + 1)
        run.save()
        try:
            STEP_FNS[name](run, pkg)
        except Exception as e:
            rec.update(status="failed", error=f"{type(e).__name__}: {e}", ended_at=_now(),
                       seconds=round(time.time() - t0, 1), traceback=traceback.format_exc()[-2000:])
            run.save()
            _write_cost_row(run, f"failed at {name}")
            print(f"  {name} FAILED: {e}\n  state kept in {run.dir.relative_to(ROOT)}; rerun to resume")
            return None
        rec.update(status="done", ended_at=_now(), seconds=round(time.time() - t0, 1))
        rec.pop("error", None)
        run.save()
    _write_cost_row(run, "finalized")
    return run.slug


COST_FIELDS = ["slug", "finished_at", "model", "steps", "llm_calls", "input_tokens_uncached", "cache_read_tokens",
               "cache_write_tokens", "output_tokens", "usd", "wall_seconds", "status"]


def _write_cost_row(run, status):
    """One row per source holding its running total. The run's ledger is cumulative, so appending on
    every rerun (a resume, a human's feedback) would count the same calls twice. A failed run writes
    a row too: it spent real money."""
    import csv

    calls = run.state["llm_calls"]
    path = OUTPUTS / "onboarding_costs.csv"
    OUTPUTS.mkdir(exist_ok=True)
    rows = list(csv.DictReader(open(path, newline="", encoding="utf-8"))) if path.exists() else []
    row = {"slug": run.slug, "finished_at": _now(), "model": llm.MODEL, "steps": len(STEPS), "llm_calls": len(calls),
           "input_tokens_uncached": sum(c["input_tokens"] for c in calls),
           "cache_read_tokens": sum(c["cache_read_input_tokens"] for c in calls),
           "cache_write_tokens": sum(c["cache_creation_input_tokens"] for c in calls),
           "output_tokens": sum(c["output_tokens"] for c in calls),
           "usd": round(sum(c["usd"] or 0 for c in calls), 4),
           "wall_seconds": round(sum(s.get("seconds", 0) for s in run.state["steps"].values()), 1),
           "status": status}
    rows = [r for r in rows if r["slug"] != run.slug] + [row]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COST_FIELDS, restval="")
        w.writeheader()
        w.writerows(rows)
