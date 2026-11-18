"""Deterministic tools the agent calls: download a resource, describe its raw
bytes, and profile the columns of parsed records. No model calls here. These
produce the evidence the model reasons over.
"""

import hashlib
import io
import json
import random
import re
import time
import zipfile
from collections import Counter
from pathlib import Path

import requests

from . import DATA
from .ckan import USER_AGENT
from .engine import abn_valid, acn_valid

RAW_DIR = DATA / "raw"
MAX_BYTES = 150 * 1024 * 1024  # download cap (150 MB). A larger file is marked truncated and the agent rejects it.
DOWNLOAD_ATTEMPTS = 3


def download(url, resource_id):
    """Download to data/raw/. Returns (path, meta). Cached by resource id."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    # Each download leaves two files: the raw file and <resource_id>.meta.json describing it.
    # If both are already on disk the cached copy is returned and nothing is fetched. This is why
    # the committed outputs stay reproducible locally while the live file moves on (see raw_status
    # in review.py, which detects that by hash).
    meta_path = RAW_DIR / f"{resource_id}.meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        p = RAW_DIR / meta["file"]
        if p.exists():
            return p, meta
    suffix = Path(url.split("?")[0]).suffix.lower()[:8] or ".bin"
    path = RAW_DIR / f"{resource_id}{suffix}"
    for attempt in range(DOWNLOAD_ATTEMPTS):
        # The file is written in 64 KB chunks and hashed as it arrives, so a large file is never
        # held in memory. The SHA-256 is the file's fingerprint: it goes into the config as
        # raw_sha256 and is how a later change to the published file is noticed.
        h = hashlib.sha256()
        size, truncated = 0, False
        try:
            r = requests.get(url, stream=True, timeout=120, headers={"User-Agent": USER_AGENT}, allow_redirects=True)
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    if size + len(chunk) > MAX_BYTES:
                        truncated = True
                        break
                    f.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
            break
        except requests.RequestException as e:
            # Public file servers drop connections. A 4xx is an answer, so only retry everything else.
            status = getattr(getattr(e, "response", None), "status_code", None)
            if attempt == DOWNLOAD_ATTEMPTS - 1 or (status and 400 <= status < 500):
                raise
            time.sleep(2 ** attempt)
    meta = {
        "file": path.name, "url": url, "final_url": r.url, "bytes": size, "truncated": truncated,
        "content_type": r.headers.get("Content-Type"), "sha256": h.hexdigest(),
        "last_modified_header": r.headers.get("Last-Modified"),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return path, meta


def describe_bytes(path, max_rows=15):
    """What is this file, really? Format sniffing that ignores the extension."""
    data = Path(path).read_bytes()
    head = data[:4096]
    d = {"bytes": len(data), "extension": Path(path).suffix}
    # File types are recognised by their first bytes ("magic numbers"), never by name or by what
    # the catalogue claims. "PK" starts every zip, and an .xlsx is a zip with an xl/ folder inside.
    if head.startswith(b"PK"):
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            d["sniffed"] = "corrupt or truncated zip"
            return d
        names = zf.namelist()
        if any(n.startswith("xl/") for n in names):
            d["sniffed"] = "xlsx"
            d["sheets"] = _xlsx_preview(data, max_rows)
        else:
            d["sniffed"] = "zip"
            d["members"] = [{"name": i.filename, "bytes": i.file_size} for i in zf.infolist()[:50]]
        return d
    if head.startswith(b"%PDF"):
        d["sniffed"] = "pdf"
        return d
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        d["sniffed"] = "legacy xls (OLE2)"
        return d
    text = head.decode("utf-8-sig", errors="replace").lstrip()
    if text.startswith("<?xml") or (text.startswith("<") and "<html" not in text[:500].lower()):
        d["sniffed"] = "xml"
        d["tag_counts"] = _xml_tags(data)
        d["preview"] = data[:2000].decode("utf-8", errors="replace")
        return d
    if "<html" in text[:1000].lower():
        d["sniffed"] = "html (probably a landing page, not data)"
        d["preview"] = text[:800]
        return d
    if text.startswith("{") or text.startswith("["):
        d["sniffed"] = "json"
        try:
            d["skeleton"] = _skeleton(json.loads(data.decode("utf-8-sig")))
        except ValueError as e:
            d["json_error"] = str(e)
            d["preview"] = text[:1500]
        return d
    # Anything else is treated as delimited text. The model is shown the first lines and how often
    # each candidate delimiter appears in line 1, and it decides the delimiter and header row.
    d["sniffed"] = "delimited text"
    lines = data[:65536].decode("utf-8-sig", errors="replace").splitlines()
    d["first_lines"] = lines[:max_rows]
    d["delimiter_counts"] = {repr(c): lines[0].count(c) for c in [",", "\t", ";", "|"]} if lines else {}
    return d


def _xlsx_preview(data, max_rows):
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets[:5]:
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= max_rows:
                break
            rows.append([("" if v is None else str(v))[:60] for v in row[:30]])
        out.append({"name": ws.title, "first_rows": rows})
    return out


def _xml_tags(data, limit=2_000_000):
    tags = Counter(re.findall(rb"<([A-Za-z_][\w:.-]*)[\s>/]", data[:limit]))
    return {k.decode(): v for k, v in tags.most_common(25)}


def _skeleton(obj, depth=0):
    # The shape of a JSON document without its data: keys, value types, list lengths, and one
    # example string. Enough for the model to find where the list of records sits (records_path).
    if depth > 4:
        return "..."
    if isinstance(obj, dict):
        return {k: _skeleton(v, depth + 1) for k, v in list(obj.items())[:30]}
    if isinstance(obj, list):
        return [f"list[{len(obj)}]", _skeleton(obj[0], depth + 1)] if obj else ["list[0]"]
    return type(obj).__name__ if not isinstance(obj, str) else f"str e.g. {obj[:40]!r}"


# Tests run on every non-empty value of every column. The model is given the share of values that
# pass each one (pattern_rates), so "this column is 99% checksum-valid ABNs" is measured evidence,
# not something it has to judge from 25 sample rows.
PATTERNS = {
    "abn_checksum_valid": lambda s: abn_valid(re.sub(r"\D", "", s)) if len(re.sub(r"\D", "", s)) == 11 else False,
    "acn_checksum_valid": lambda s: acn_valid(re.sub(r"\D", "", s)) if len(re.sub(r"\D", "", s)) == 9 else False,
    "postcode_like": lambda s: bool(re.fullmatch(r"\d{3,4}", s.strip())),
    "date_like": lambda s: bool(re.search(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|\d{4}-\d{2}-\d{2}T", s)),
    "state_like": lambda s: s.strip().upper() in {"NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT"},
    "url_like": lambda s: bool(re.match(r"(https?://|www\.)", s.strip(), re.I)),
    "email_like": lambda s: "@" in s and "." in s.split("@")[-1],
    "company_suffix": lambda s: bool(re.search(r"\b(PTY|LTD|LIMITED|INC|INCORPORATED)\b", s.upper())),
}


def profile_columns(records, examples=5, seed=0):
    """Per-column statistics: how full the column is, how many distinct values, the 5 most common
    values, 5 random examples, and the pattern rates. This is the model's main evidence when it
    proposes a mapping."""
    rng = random.Random(seed)
    cols = []
    for r in records:
        for k in r:
            if k not in cols:
                cols.append(k)
    out = []
    n = len(records)
    for c in cols:
        vals = [str(r.get(c, "")).strip() for r in records]
        nonempty = [v for v in vals if v]
        counts = Counter(nonempty)
        prof = {
            "column": c,
            "non_empty_rate": round(len(nonempty) / n, 3) if n else 0,
            "distinct": len(counts),
            "top_values": [[v[:60], k] for v, k in counts.most_common(5)],
            "examples": [v[:80] for v in rng.sample(nonempty, min(examples, len(nonempty)))],
        }
        if nonempty:
            hits = {name: round(sum(fn(v) for v in nonempty) / len(nonempty), 3) for name, fn in PATTERNS.items()}
            prof["pattern_rates"] = {k: v for k, v in hits.items() if v > 0}
        out.append(prof)
    return out


# describe_bytes shows the model the top of the file in the inspect step, so those rows are not
# unseen. Generous, because an XML or error preview is measured in bytes rather than rows.
INSPECT_SEEN_ROWS = 25


def reservoir_sample(records, k, seed=1, skip=0):
    """A uniform random k rows from anywhere in a stream, in stream order, plus the total count.
    Reading only the top of a file would miss whatever changes further down it.

    The first `skip` rows stay out of the pool, unless the file is so small that they are most of it."""
    # Reservoir sampling: one pass, memory for only k rows, no need to know the length in advance.
    # The first k rows fill the reservoir. After that, row number i (counting from 0) replaces a
    # random slot with probability k / (i + 1), which leaves every row equally likely to be kept.
    rng = random.Random(seed)
    top, picked, total = [], [], 0
    for rec in records:
        total += 1
        if len(top) < skip:
            top.append(rec)
            continue
        i = total - 1 - len(top)  # this row's position among the rows eligible for sampling
        if len(picked) < k:
            picked.append((i, rec))
        else:
            j = rng.randint(0, i)
            if j < k:
                picked[j] = (i, rec)
    picked.sort(key=lambda p: p[0])
    rows = [r for _, r in picked]
    if len(rows) < 2 * skip:
        rows = top + rows
    return rows, total


def split_records(records, sample_n=25, holdout_n=500, final_n=500, seed=1):
    """Shuffle once with a fixed seed and cut into four sets that do not overlap:

      sample      the rows the model reads
      holdout     used in the revise loop; the model sees error examples and previews from it
      final_test  never shown to the model at any point; scored once, after revising stops
      profile     sample + everything else, the only rows column statistics are computed over

    A small file gives up its profile rows first, then shrinks holdout and final_test equally. The
    sample never takes more than half the file: with all rows in the sample there is nothing left to test."""
    # Row positions are shuffled, not the rows, and the sets are stored as lists of positions. The
    # agent saves those lists in its run state, so a resumed run gets exactly the same split.
    idx = list(range(len(records)))
    random.Random(seed).shuffle(idx)
    sample_n = min(sample_n, max(1, len(records) // 2))
    sample_idx = idx[:sample_n]
    rest = idx[sample_n:]
    # Holdout and final test take up to 500 rows each, but never more than 40% each of what is left.
    h = min(holdout_n, int(len(rest) * 0.4))
    f = min(final_n, int(len(rest) * 0.4))
    holdout_idx, final_idx = rest[:h], rest[h:h + f]
    profile_idx = sample_idx + rest[h + f:]
    return {"sample_idx": sample_idx, "holdout_idx": holdout_idx, "final_test_idx": final_idx,
            "profile_idx": profile_idx}
