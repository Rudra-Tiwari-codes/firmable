"""Part 1: discovery and triage. Deterministic, with no model calls.

Pipeline:
  1. pool   - package_search over a fixed list of queries plus one page of
              recently modified datasets, deduplicated by package id.
  2. text   - score each package on title, description, tags, formats and
              publisher. This is cheap and runs over the whole pool.
  3. sniff  - for the top-N text candidates, read column headers
              (datastore field list, or the first 16 KB of a CSV). Columns
              are much stronger evidence than titles.
  4. rank   - final score = text + header score, de-duplicate dataset
              series, cap per publisher, take 50.
"""

import csv
import hashlib
import io
import json
import math
import random
import re
from concurrent.futures import ThreadPoolExecutor

import requests

from . import CHECKS, DATA, OUTPUTS
from .ckan import CKAN, USER_AGENT

QUERIES = [
    "ABN", "ACN", "business names", "company register", "licence holders",
    "licensees", "licensed premises", "liquor licence", "builders licence",
    "contractors", "suppliers", "contracts awarded", "procurement", "tenders",
    "grants awarded", "grant recipients", "employers", "charities",
    "incorporated associations", "registered training organisations",
    "approved providers", "food business", "penalty notices", "prosecutions",
    "enforcement action", "permits issued", "operators", "retailers",
    "business", "companies",
]
ROWS_PER_QUERY = 50   # results kept per search query (30 queries x 50, before de-duplication)
RECENT_ROWS = 300     # plus this many recently modified datasets on any topic, so the pool is not only
                      # things the queries were written to find: the scorer has real noise to reject
SNIFF_TOP_N = 150     # only the best 150 by text score get their column headers read (network calls are slow)
SNIFF_WORKERS = 8     # header reads running at once
SHORTLIST_N = 50      # size of the deliverable
MAX_PER_ORG = 5       # no publisher may take more than 5 of the 50 places
SAMPLE_N = 20         # rows drawn for the human hand-check
SAMPLE_SEED = 42      # fixed seed: the same 20 are drawn on every run, so the check is reproducible
VERDICT_COL = "contains_business_entities (y/n)"

# (regex, weight, label). Matched once per field (presence, not frequency) so
# a long description cannot win on repetition.
POSITIVE = [
    (r"\babns?\b|australian business number", 3.0, "ABN"),
    (r"\bacns?\b|australian company number", 3.0, "ACN"),
    (r"business names?", 2.0, "business name"),
    (r"\bcompan(y|ies)\b", 1.5, "company"),
    (r"licen[cs]ees?|licen[cs]e holders?", 2.5, "licensee"),
    (r"\bsuppliers?\b|\bvendors?\b", 2.0, "supplier"),
    (r"\bcontractors?\b|\btenderers?\b", 2.0, "contractor"),
    (r"contracts? awarded|procurement|\btenders?\b", 1.5, "procurement"),
    (r"grant recipients?|grants? awarded|funding recipients?", 1.5, "grant recipient"),
    (r"\bemployers?\b", 1.5, "employer"),
    (r"\bcharit(y|ies)\b", 1.5, "charity"),
    (r"incorporated associations?", 2.0, "inc. association"),
    (r"\bregisters?\b|\bregistered\b", 1.0, "register"),
    (r"\blicen[cs]es?\b|\blicen[cs]ed\b", 1.0, "licence"),
    (r"\boperators?\b|\bproviders?\b", 1.0, "operator/provider"),
    (r"\bbusiness(es)?\b|\btrading\b", 1.0, "business"),
    (r"\bretailers?\b|\bpremises\b", 1.0, "retail/premises"),
    (r"penalty notices?|prosecutions?|enforcement|infringements?", 1.0, "enforcement"),
    (r"\bpermits?\b", 0.7, "permit"),
]
NEGATIVE = [
    (r"\bboundar(y|ies)\b", -2.0, "boundaries"),
    (r"rainfall|climate|weather|bathymetry|elevation|lidar|imagery|aerial", -2.0, "environmental/spatial"),
    (r"\bspecies\b|vegetation|flora|fauna|habitat|geolog|soil|groundwater", -2.0, "environmental"),
    (r"\bcensus\b|\bpopulation\b", -1.5, "population stats"),
    (r"statistic|aggregat|\bsummary\b|\btotals?\b", -1.0, "aggregate"),
    (r"\bbudget\b|\bportfolio\b", -0.7, "budget"),
]
# Publishers whose output is very often entity-level. A small nudge only.
ORG_PRIOR = [
    (r"securities and investments|\basic\b", 1.0),
    (r"business register|australian business register|\babr\b", 1.0),
    (r"charities and not-for-profits|\bacnc\b", 1.0),
    (r"fair trading|consumer affairs", 0.7),
    (r"finance", 0.3),
]
# Formats that can hold rows of records. A dataset with none of these (PDF only, say) loses points.
TABULAR = {"CSV", "XLSX", "XLS", "JSON", "XML", "API", "GEOJSON", "TSV", "ZIP", "TXT"}

# Column-header evidence. Three kinds of column are looked for: an identifier (ABN, ACN),
# a business-name column, and an address column.
HDR_ID = re.compile(r"\babn\b|\bacn\b|business.?num|company.?num|\bnzbn\b", re.I)
HDR_NAME = re.compile(
    r"(business|company|trading|legal|entity|registered|organi[sz]ation|operator|supplier|vendor|"
    r"contractor|employer|licen[cs]ee|recipient|tenderer|provider|holder|owner).?name"
    r"|licen[cs]ee|supplier|vendor|contractor|employer|recipient|tenderer",
    re.I,
)
HDR_ADDR = re.compile(r"address|postcode|post.?code|suburb|locality", re.I)


def _text(pkg):
    tags = " ".join(t.get("name", "") for t in pkg.get("tags") or [])
    return (pkg.get("title") or ""), f"{pkg.get('notes') or ''} {tags}"


def formats(pkg):
    out = []
    for r in pkg.get("resources") or []:
        f = (r.get("format") or "").strip().upper()
        if f and f not in out:
            out.append(f)
    return out


def text_score(pkg):
    """Stage 2: score a dataset from its catalogue text alone. Returns (score, hits), where hits is
    a list of (points, label) pairs. The labels later become the one-line reason in the shortlist."""
    title, body = _text(pkg)
    score, hits = 0.0, []
    for pat, w, label in POSITIVE + NEGATIVE:
        rx = re.compile(pat, re.I)
        in_title, in_body = bool(rx.search(title)), bool(rx.search(body))
        if in_title or in_body:
            # Title hits count double: titles are short and deliberate.
            s = w * (2 if in_title else 1)
            score += s
            hits.append((s, f"{'title' if in_title else 'desc'}:{label}"))
    org = ((pkg.get("organization") or {}).get("title") or "")
    # At most one publisher bonus: the loop stops at the first pattern that matches.
    for pat, w in ORG_PRIOR:
        if re.search(pat, org, re.I):
            score += w
            hits.append((w, "publisher prior"))
            break
    fmts = set(formats(pkg))
    # +1 if any resource is in a row-shaped format, otherwise -2.
    if fmts & TABULAR:
        score += 1.0
    else:
        score -= 2.0
        hits.append((-2.0, "no tabular resource"))
    return score, hits


def _sniff_csv_header(url, session):
    """Read only the first line of a CSV. The Range header asks the server for the first 16 KB
    instead of the whole file, which may be hundreds of megabytes. Returns the column names, or
    None if that failed. Either answer is cached, so a dead link is not retried on every run."""
    cache = DATA / "cache" / "headers" / (hashlib.sha1(url.encode()).hexdigest() + ".json")
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    cols = None
    try:
        r = session.get(url, headers={"Range": "bytes=0-16383"}, timeout=20, stream=True)
        if r.status_code in (200, 206):
            raw = r.raw.read(16384, decode_content=True)
            text = raw.decode("utf-8-sig", errors="replace")
            first = text.splitlines()[0] if text else ""
            # A "CSV" link that answers with a web page (a login or landing page) has no header to read.
            if first and "<html" not in first.lower():
                cols = next(csv.reader(io.StringIO(first)))
        r.close()
    except Exception:
        cols = None
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(cols), encoding="utf-8")
    return cols


def sniff_columns(pkg, ckan, session, max_resources=3):
    """Column names from up to `max_resources` tabular resources."""
    cols = []
    # Cheapest sources of column names first: datastore-backed resources (one small API call),
    # then CSVs. False sorts before True, which is why both sort keys are written as negatives.
    resources = sorted(
        pkg.get("resources") or [],
        key=lambda r: (not r.get("datastore_active"), (r.get("format") or "").upper() != "CSV"),
    )
    for r in resources[:max_resources]:
        got = None
        # Try the datastore field list; fall back to reading the first bytes of a CSV.
        if r.get("datastore_active"):
            got = ckan.datastore_fields(r["id"])
        if not got and (r.get("format") or "").upper() == "CSV" and r.get("url"):
            got = _sniff_csv_header(r["url"], session)
        if got:
            cols.extend(c for c in got if c not in cols)
    return cols


def header_score(cols):
    """Stage 3: score the real column names. +5 for an identifier column, +3 for a business-name
    column, +1 for an address column; -2 if columns were read and none of them names an entity."""
    # No columns read (not sniffed, or sniffing failed) is not evidence either way: score 0.
    if not cols:
        return 0.0, []
    joined = [str(c) for c in cols]
    score, hits = 0.0, []
    ids = [c for c in joined if HDR_ID.search(c)]
    names = [c for c in joined if HDR_NAME.search(c)]
    addrs = [c for c in joined if HDR_ADDR.search(c)]
    if ids:
        score += 5.0
        hits.append((5.0, f"column:{ids[0]}"))
    if names:
        score += 3.0
        hits.append((3.0, f"column:{names[0]}"))
    if addrs:
        score += 1.0
        hits.append((1.0, f"column:{addrs[0]}"))
    if not (ids or names):
        # We looked at real columns and found nothing entity-like: that is evidence too.
        score -= 2.0
        hits.append((-2.0, "columns look non-entity"))
    return score, hits


def confidence(score):
    # Logistic squash. Midpoint 6 is roughly "one strong title term plus one
    # entity column". Not calibrated: the hand-check sample is the calibration.
    # Capped at 0.99: a keyword score cannot justify printing certainty.
    return min(round(1 / (1 + math.exp(-(score - 6) / 2.0)), 2), 0.99)


def series_key(title):
    """A title with its years, months and period words removed. "Contractors 2022-23" and
    "Contractors 2023-24" give the same key, so only the best-scoring one of a series is shortlisted."""
    t = title.lower()
    t = re.sub(r"\b(19|20)\d{2}(-\d{2,4})?\b", " ", t)
    t = re.sub(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", " ", t)
    t = re.sub(r"\b(q[1-4]|quarter|monthly|annual|financial year|fy)\b", " ", t)
    t = re.sub(r"[^a-z]+", " ", t)
    return t.strip()


def build_pool(ckan):
    """Stage 1: the pool of catalogue records, keyed by package id so a dataset found by several
    queries is held once. Each entry also remembers which queries found it."""
    pool = {}
    for q in QUERIES:
        res = ckan.package_search(q=q, rows=ROWS_PER_QUERY)
        for pkg in res["results"]:
            pool.setdefault(pkg["id"], {"pkg": pkg, "queries": []})["queries"].append(q)
    res = ckan.package_search(q="*:*", rows=RECENT_ROWS, sort="metadata_modified desc")
    for pkg in res["results"]:
        pool.setdefault(pkg["id"], {"pkg": pkg, "queries": []})["queries"].append("<recent>")
    return pool


def run(use_cache=True):
    ckan = CKAN(use_cache=use_cache)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    pool = build_pool(ckan)
    print(f"pool: {len(pool)} unique packages")

    # Text-score every dataset in the pool, best first. This needs no network calls.
    rows = []
    for pid, entry in pool.items():
        pkg = entry["pkg"]
        s, hits = text_score(pkg)
        rows.append({"pkg": pkg, "queries": entry["queries"], "text_score": s, "hits": hits})
    rows.sort(key=lambda r: r["text_score"], reverse=True)

    # Each package is sniffed independently, so running them concurrently cannot change the result.
    # One at a time this took about 10 minutes uncached, mostly waiting on slow publisher servers.
    with ThreadPoolExecutor(max_workers=SNIFF_WORKERS) as pool:
        sniffed = pool.map(lambda r: sniff_columns(r["pkg"], ckan, session), rows[:SNIFF_TOP_N])
        for i, (r, cols) in enumerate(zip(rows[:SNIFF_TOP_N], sniffed)):
            hs, hh = header_score(cols)
            r["columns"] = cols
            r["header_score"] = hs
            r["hits"] = r["hits"] + hh
            if (i + 1) % 25 == 0:
                print(f"  sniffed {i + 1}/{SNIFF_TOP_N}")
    # Datasets below the top 150 were never sniffed: they keep a header score of 0.
    # Final score = text score + header score, and the pool is re-ranked on it.
    for r in rows:
        r.setdefault("columns", None)
        r.setdefault("header_score", 0.0)
        r["score"] = r["text_score"] + r["header_score"]
    rows.sort(key=lambda r: r["score"], reverse=True)

    OUTPUTS.mkdir(exist_ok=True)
    with open(OUTPUTS / "triage_pool.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(_record(r, full=True)) + "\n")

    # Stage 4: walk down the ranking and take the first 50 that pass two diversity rules:
    # one dataset per series, and at most MAX_PER_ORG per publisher.
    shortlist, seen_series, per_org = [], set(), {}
    for r in rows:
        pkg = r["pkg"]
        key = series_key(pkg.get("title") or "")
        org = (pkg.get("organization") or {}).get("title") or "unknown"
        if key in seen_series or per_org.get(org, 0) >= MAX_PER_ORG:
            continue
        seen_series.add(key)
        per_org[org] = per_org.get(org, 0) + 1
        shortlist.append(r)
        if len(shortlist) == SHORTLIST_N:
            break

    fields = ["rank", "dataset_id", "name", "title", "organisation", "formats",
              "confidence", "reason", "score", "licence", "url"]
    with open(OUTPUTS / "shortlist.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for i, r in enumerate(shortlist, 1):
            rec = _record(r)
            rec["rank"] = i
            w.writerow({k: rec[k] for k in fields})
    print(f"shortlist: {len(shortlist)} -> outputs/shortlist.csv")

    _write_sample(shortlist)
    return shortlist


def _record(r, full=False):
    pkg = r["pkg"]
    # The "reason" column is the 4 signals that moved the score most, in either direction.
    top = sorted(r["hits"], key=lambda h: -abs(h[0]))[:4]
    rec = {
        "dataset_id": pkg["id"],
        "name": pkg.get("name"),
        "title": pkg.get("title"),
        "organisation": (pkg.get("organization") or {}).get("title"),
        "formats": "|".join(formats(pkg)),
        "confidence": confidence(r.get("score", r["text_score"])),
        "reason": "; ".join(h[1] for h in top) or "no signals",
        "score": round(r.get("score", r["text_score"]), 2),
        "licence": pkg.get("license_title") or pkg.get("license_id") or "unknown",
        "url": f"https://data.gov.au/data/dataset/{pkg.get('name')}",
    }
    if full:
        rec["text_score"] = round(r["text_score"], 2)
        rec["header_score"] = r.get("header_score", 0.0)
        rec["columns"] = r.get("columns")
        rec["queries"] = r["queries"]
    return rec


def _write_sample(shortlist):
    """Random 20 of the 50 for the human hand-check. Never overwrites a filled-in file."""
    CHECKS.mkdir(exist_ok=True)
    path = CHECKS / "shortlist_reviewed.csv"
    if path.exists() and any(r.get(VERDICT_COL, "").strip() for r in _read_sheet(path)):
        print(f"{path.name} has answers, leaving it alone")
        return
    sample = random.Random(SAMPLE_SEED).sample(shortlist, min(SAMPLE_N, len(shortlist)))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["dataset_id", "title", "url", "confidence",
                    "contains_business_entities (y/n)", "reviewer_notes"])
        for r in sample:
            rec = _record(r)
            w.writerow([rec["dataset_id"], rec["title"], rec["url"], rec["confidence"], "", ""])
    print(f"hand-check sample: {path} (fill in the y/n column)")


def _read_sheet(path):
    """Spreadsheet programs on Windows save CSV as UTF-8 with a BOM or as cp1252. Accept both."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    return list(csv.DictReader(io.StringIO(text, newline="")))


def report():
    """False positive rate from the filled-in sheet: the share of checked rows a person marked "n".
    Rows left blank are not counted in either direction."""
    path = CHECKS / "shortlist_reviewed.csv"
    rows = _read_sheet(path)
    # Only the first letter counts, so "y", "Y" and "yes" all read as yes.
    answers = [r[VERDICT_COL].strip().lower()[:1] for r in rows]
    done = [a for a in answers if a in ("y", "n")]
    yes = done.count("y")
    if not done:
        print("no rows hand-checked yet")
        return None
    out = {"checked": len(done), "of": len(rows), "contain_business_entities": yes, "false_positives": len(done) - yes,
           "false_positive_rate": round((len(done) - yes) / len(done), 3)}
    (OUTPUTS / "shortlist_false_positive_rate.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"hand-checked {len(done)}/{len(rows)}: {yes} contain business entities, "
          f"{len(done) - yes} false positives (FP rate {(len(done) - yes) / len(done):.0%})")
    return out
