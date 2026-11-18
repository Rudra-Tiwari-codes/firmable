# Write-up

Built with Claude Code. This is where I stopped; section 6 says what I would do next.

## Measured results

I did the hand-checks myself: I opened each dataset and looked up each linked ABN on ABN Lookup, using AI assistance only to open the pages.

| Measure | Result |
|---|---|
| Part 1 false positives | 3 of 17 (18%). 3 of the 20 could not be judged (dead file, dead host, API page only). |
| Part 3 precision at 0.80 | 50 of 50, 95% interval 93–100% |
| By tier | Exact ABN 42/42; name and postcode 8/8 (68–100%) |
| Refused band, 0.60–0.80 | 23 of 25 were the same business |
| Recall | Not measured; there is no labelled set |

423,390 records → 204,190 source entities → 946 scored pairs → 656 links → 654 entities, with 380 refusals queued. Confidences are hand-set bands, not probabilities.

The refused band is the finding. All 22 name-only pairs in it were correct, and they were refused only because their postcodes differed (a charity's office against its venue). The postcode rule costs real recall, and loosening it trades against generic names like "Palm Beach Surf Life Saving Club". Only 8 name-tier links were checked, too few to prove that tier is safe.

## 1. Cost

- **One-time, per source:** mean $0.33 ($0.13 to $0.49). That is 3 to 7 model calls, 27k to 85k input and 9k to 35k output tokens, and 1 to 3.5 minutes. Six sources cost $1.96.
- **Per record: $0.** Everything after onboarding is deterministic. Linking 423,390 records takes 1 to 2 minutes.
- Costs are API usage times list price. Output tokens are 71% of them, so revise rounds drive cost. Reviewer time is the real per-source cost, and I did not measure it.

## 2. Agent design

- **Steps:** select resource (model), download, inspect (model), load, profile, propose (model), validate and revise (model, up to 3 rounds), finalize. Each split marks a different kind of failure.
- **Self-checks:** reader errors go back to the model. Every mapping runs on held-out rows and the report goes back too. 4 of 6 configs failed first validation and fixed themselves.
- **Failure:** state is saved after every step, so a rerun resumes without repeating paid calls.
- **Human gate:** a packet per source shows each mapping's coverage and error rate, what was left unmapped and why, the agent's changes, and previews. Unapproved or edited configs cannot run. I wasn't sure how much of the approval was mine to do, so I had Claude review and approve the six configs, as they record. I have since read all six packets myself.
- **Weakest step: the validator checks parsing, not truth** (see ACT). On a trial source, one column's confidence also fell from 0.88 to 0.70 over three rounds with no new data. Mapping confidence is an opinion, not a measurement.
- **A correction:** the runs behind the six configs leaked. Column profiles included holdout rows, and the holdout came only from each file's first 5,000 rows. New runs sample the whole file and keep a final test the model never sees. I measured the six over every row with `audit`: no mapping exceeds the 5% error limit.

## 3. The hard one: ACT invoices

ACT looked easiest: it passed first try at the lowest cost, with a 0.1% error rate. It is the dirtiest source. 609 of 5,142 ABNs carry unrelated names, and 535 names sit under several ABNs. A checksum proves the number is real, not that it belongs to that row's supplier.

I wrote no custom code. The validator now reports this, the matcher refuses an ABN match when no name agrees and the ABN is inconsistent, and the real fix is an ABR check. My rule: a check other sources would reuse goes in the engine (`zfill` came from ASIC's mixed ABN/ACN column). Custom code is right only for logic nothing else would reuse.

## 4. Scale: 6 to 500 sources

1. **Linking fails first.** It holds everything in memory, re-reads every source on each run, and has no incremental update. My untested guess is it runs out of memory well before 500.
2. **Then reviewer attention.** Money is not the limit: 500 × $0.33 ≈ $165.
3. **Then drift.** Three of six files changed within a week of approval, and nothing re-onboards automatically.

## 5. Change: replacing the matcher

**Record in advance** (all now in the outputs):
- stable link ids
- matcher, blocking and extractor versions on every link
- each link's features
- every scored pair, including refusals
- identifier-derived entity keys
- raw file hashes
- human verdicts tied to link and matcher version

**Then:**
1. Run the new matcher in shadow.
2. Diff by link id, and review only disagreements.
3. Measure a sample of them, and replay past human verdicts as regression tests.
4. Promote tier by tier, superseding links rather than deleting them, so rollback is a flag.
5. Recompute only the changed entities, and publish those changes downstream.

I did this once for real. `rules-0.1.0` to `rules-0.2.0` kept 581 of 661 links, moved 72, removed 8 and added 3. The removals include two wrong merges: the University of Newcastle joined to its venue operators.

**Still missing:**
- raw snapshots
- a versioned link store
- calibrated scores
- a recall check when blocking changes

## 6. Next, with three weeks

1. A labelled set, to measure recall and calibrate scores.
2. ABR checks on name and ABN pairs.
3. A versioned link store with incremental linking.
4. A review UI aimed at disagreements.

## How I modelled relationships

Pairwise links between source entities, entities as connected components, and typed edges that never merge. A pair is what a person can check and a new matcher can re-score, while a cluster hides which claim caused a merge. Merging two different ABNs or ACNs is refused. `trustee_of` (253 edges) stays an edge because a trustee and its trust are separate entities. No source states parent or subsidiary.

## Gaps

ACT's licence is "Other", flagged but unresolved. QBCC names individuals. `status`, `industry_code` and `date_registered` are never filled. 49 of 50 profiles span two sources, so deleting a source mostly removes links rather than changing values.
