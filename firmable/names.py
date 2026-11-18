"""Name normalisation and comparison. Shared by the validator, the linker and the profiler."""

import re
from difflib import SequenceMatcher

# Words that say what kind of legal entity something is, not what it is called. They are removed
# before comparing, so "Acme Pty Ltd", "ACME PTY. LIMITED" and "Acme" all compare equal.
LEGAL_TOKENS = {"PTY", "PROPRIETARY", "LTD", "LIMITED", "PL", "INC", "INCORPORATED", "CO", "COMPANY",
                "CORP", "CORPORATION", "LLC", "PLC", "NL", "THE"}
# Matches " T/A ...", " T/AS ..." or " TRADING AS ..." to the end of the string, so
# "SMITH PTY LTD T/A SMITH'S CAFE" is cut back to the legal name before comparing.
_TRADING = re.compile(r"\s+(?:T/A|T/AS|TRADING AS)\s+.*$", re.I)


def norm_name(s):
    """Uppercase, '&' -> AND, drop punctuation, legal-form tokens and 'THE TRUSTEE FOR'."""
    if not s:
        return ""
    s = _TRADING.sub("", s.upper())
    s = re.sub(r"^THE TRUSTEE FOR\s+", "", s)
    s = s.replace("&", " AND ").replace("P/L", " PL ")
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    toks = [t for t in s.split() if t not in LEGAL_TOKENS]
    return " ".join(toks)


def legal_form(s):
    """The legal form a raw name states: 'company', 'incorporated', or None if it states none.
    norm_name strips these tokens so spellings compare equal, which is also why 'X Limited' and
    'X Incorporated' look identical after normalising. They are different legal entities."""
    u = f" {re.sub(r'[^A-Z0-9 ]+', ' ', (s or '').upper())} "
    if re.search(r" (PTY|PROPRIETARY|LTD|LIMITED) ", u):
        return "company"
    if re.search(r" (INC|INCORPORATED) ", u):
        return "incorporated"
    return None


def name_sim(a, b):
    """Similarity of two normalised names, from 0 to 1 (1 = identical). None if either is empty,
    because "no name to compare" is different from "the names disagree". Two measures are taken
    and the higher one is used:
      Jaccard on words    shared words / all words. Ignores word order.
      character ratio     difflib's share of matching characters. Tolerates typos.
    A pair that is close in either way counts as close."""
    if not a or not b:
        return None
    ta, tb = set(a.split()), set(b.split())
    jac = len(ta & tb) / len(ta | tb)
    return round(max(jac, SequenceMatcher(None, a, b).ratio()), 3)


def best_sim(names_a, names_b):
    """Highest similarity over all name pairs. Sorted, so a tie gives the same pair on every run."""
    best, pair = None, None
    for x in sorted(names_a):
        for y in sorted(names_b):
            s = name_sim(x, y)
            if s is not None and (best is None or s > best):
                best, pair = s, (x, y)
    return best, pair


def same_name_variant(a, b):
    """True when two normalised names are spellings of one name: same tokens in another order
    ('GOYAL PANKAJ' / 'PANKAJ GOYAL'), the same letters spaced differently ('T K ELEVATOR' /
    'TK ELEVATOR'), or a near-identical string (a typo)."""
    if a == b:
        return True
    if not a or not b:
        return False
    if sorted(a.split()) == sorted(b.split()) or a.replace(" ", "") == b.replace(" ", ""):
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.92
