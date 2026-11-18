# Firmable take-home: agentic schema inference and entity identification

**Status.** Parts 1 to 5 are built and run, and the hand-checks are done. Results:

- **Shortlist:** 3 false positives in 17 checkable datasets (18%).
- **Links:** precision 50 of 50 at 0.80 (95% interval 93–100%).
- **Refused band:** 23 of 25 pairs between 0.60 and 0.80 were the same business.

The six config approvals were made by Claude on the author's instruction. The review records say so, and `WRITEUP.md` explains why.

This file says what works and what doesn't, as it stands.

## Run it from a clean checkout

Python 3.11+ (developed on 3.12, Windows). Parts 1, 3 and 4 need no API key. Measured once on a laptop from a fresh copy, with other work running: about 9 minutes for install, `triage`, `extract`, `link` and `profiles` (3 of those are `pip install`). The tests and `audit` add about 2.5 minutes.

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows   (source .venv/bin/activate on macOS/Linux)
pip install -r requirements-dev.txt
python -m pytest tests            # about 30 s, offline, no API key: the model and catalogue are faked
```

**Your numbers will differ from the committed outputs.** `data/raw/` is not in the repo, so a clean run downloads the live files, and at least three of the six (ACNC, ASIC, QBCC) have changed since approval. The run still completes. It prints a warning and records `source_drifted: true` in `outputs/links/summary.json` and each `*.stats.json`. A clean run also overwrites the committed outputs, so use `git diff` to see what moved, and don't rerun `triage` or `link` if you want the committed numbers to stay in place.

### Part 1: triage (no API key, about 1 minute)

```bash
python -m firmable triage          # -> outputs/shortlist.csv, outputs/triage_pool.jsonl, checks/shortlist_reviewed.csv
python -m firmable triage-report   # false positive rate from the filled-in hand-check
```

### Part 2: agent onboarding, human review, extraction

Onboarding needs `cp .env.example .env` and an `ANTHROPIC_API_KEY`. Each source costs about $0.13 to $0.49, and `LLM_BUDGET_USD` is a hard cap per process.

```bash
# The agent takes only a dataset id or name. It picks the resource, reads it, and writes the config.
python -m firmable onboard asic-credit-licensee
python -m firmable onboard kzmf-7uhp --resource 5cf1e4ea-7509-4951-b235-11204fc7d3d5   # pin the JSON resource

python -m firmable review asic-credit-licensee                  # interactive: shows the packet, asks a/r/f
python -m firmable review asic-credit-licensee --approve --reviewer "Name"
python -m firmable review asic-credit-licensee --feedback "ABN_ACN mixes ACNs in; split it"

python -m firmable extract         # every approved config, 1,000 records each (about 30 s with downloads)
python -m firmable audit           # every config over its whole file, no model calls -> outputs/audit/ (1-2 minutes)
```

To test the seventh-dataset claim, run `onboard` on any shortlisted dataset that has no config, for example `fair-jobs-code-registers` (9,334 rows, about $0.24) or `junior-minerals-exploration-incentive-credit-allocations` (a multi-sheet XLSX, about $0.24 with one round of feedback). Both were run live in a scratch copy and are not part of the submission.

### Part 3: entity identification, Part 4: profiles (no API key)

```bash
python -m firmable link            # 1-2.5 minutes -> outputs/links/, and blank sheets in checks/
python -m firmable link-report --reviewer "Name"     # precision from the filled-in sheets -> outputs/links/precision.json
python -m firmable link-diff outputs/links/history/links.rules-0.1.0.jsonl   # what a matcher change did, link by link
python -m firmable profiles        # about 1 minute -> outputs/profiles/
```

`extract`, `link` and `profiles` only use configs that are approved and unedited since approval. Add `--allow-unapproved` to run them anyway.

A failed onboarding run keeps its state in `data/runs/<slug>/`, exits nonzero and records what it spent. Rerunning the same command resumes at the first step that did not complete. `--restart-from <step>` forces a step to run again.

### Doing the hand-checks

1. Fill in the y/n column of `checks/shortlist_reviewed.csv`, `checks/links_reviewed.csv` and `checks/links_loose_reviewed.csv`. Excel and similar tools are fine. A sheet with any answers is never overwritten.
2. Run `triage-report` and `link-report`, then copy the numbers into `WRITEUP.md`.

## Layout

| Path | What |
|---|---|
| `firmable/triage.py` | Part 1: pool, deterministic scoring, header sniffing, shortlist |
| `firmable/engine.py` | **The one generic extractor**: readers, transform vocabulary, config → observations |
| `firmable/agent.py` | **The onboarding agent**: 8 steps, state saved after each one |
| `firmable/inspect_tools.py` | Deterministic tools the agent uses: download, byte sniffing, column profiling, sampling |
| `firmable/validate.py` | Runs a proposed config over held-out records and scores it |
| `firmable/audit.py` | Checks configs over their whole file, with no model calls |
| `firmable/review.py` | Human approval gate, review packet, extract command |
| `firmable/llm.py` | Model calls with a token and cost ledger and a hard budget cap |
| `firmable/names.py` | Name normalisation and comparison |
| `firmable/link.py` | Part 3: source entities, blocking, rule-based link scoring, clustering, unlinked queue, matcher diff |
| `firmable/profile.py` | Part 4: per-field resolution with confidence, provenance, disagreements; source-deletion test |
| `tests/` | Offline tests, including the agent end to end with a fake model |
| `configs/` | Agent-written mapping configs (none written by hand) |
| `data/runs/` | The agent's saved state for each source, including every config version it tried |
| `outputs/shortlist.csv` | Part 1 result |
| `outputs/review/` | One review packet per source, which is what the approver reads |
| `outputs/audit/` | Whole-file check of each config, plus licence flags and source changes |
| `outputs/observations/` | Canonical observations, one JSON line per (record, field, value) |
| `outputs/onboarding_costs.csv` | Real per-source onboarding cost and time |
| `outputs/links/` | Links, entities, memberships, unlinked queue, relationships, every scored pair, older matcher's links, matcher diff |
| `outputs/profiles/` | 50 company profiles, source-deletion results and note |
| `checks/` | Hand-check sheets (filled in by a person) |

## Decisions worth knowing about

- Part 1 is deterministic: no model calls, $0. The model budget goes to Part 2, where the agent needs it.
- One observation per (source record, canonical field, value), each carrying the full ontology envelope. `field_confidence` (the mapping) and `source_reliability` (the publisher) are separate fields. Link confidence appears only on links.
- The engine enforces the ontology as a contract. A value that breaks an enum, a pattern or a checksum is dropped and counted as an error, never emitted.
- `extract` refuses to run a config that is unapproved, or that was edited after approval (checked by hash).
- `source_record_id`: the agent picks key columns when the source has a stable key. Otherwise it is `h:` + sha1 of the raw record with sorted keys. A row whose id is already taken gets `#2`, `#3` in file order.
- `observed_at`: a per-record column if the agent finds one. Otherwise the resource's CKAN `last_modified` (the date the publisher last stated it).

## Known gaps

- **Recall is not measured.** Precision rests on 50 links, of which only 8 are name-tier.
- **The six configs predate the agent fixes.** Their runs leaked holdout rows into the prompt and sampled only the first 5,000 rows, as `WRITEUP.md` explains. New runs don't. `audit` measures the six over their whole files instead. The `review` block is not part of the config hash, so approving again under another name changes no output.
- **Link confidences are hand-set bands, not probabilities.** The linker has no fuzzy address matching and no learned model. Its person check still reads some short organisation names as people, which costs links but does not create wrong ones. `rules-0.2.0` was changed after reading this data, so it may be tuned to it.
- **Only trustee relationships are extracted** ("X ATF Y"). None of the six sources state parent or subsidiary.
- **A wrong ABN next to a matching name still merges.** Only a reference such as the ABR would catch it.
- **Licences:** ACT is "Other", flagged but unresolved. NSW's metadata and description disagree (CC BY 3.0 vs 4.0). The source is a stale 2021 snapshot; a newer list ranks higher in the shortlist.
- **Privacy:** QBCC's public register names individuals, and those names are in the outputs. Think about that before making this repo public.
- `link` and `profiles` re-read every source in full, and there is no incremental update.
- Legacy `.xls` (OLE2) files are detected but not readable, and the agent reports them. A server that refuses connections stops a run, as one source did in testing (`victorian-government-schools-abns`).
