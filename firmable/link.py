"""Part 3: entity identification across the onboarded sources.

The model, from the bottom up:

  record          one source record, reduced to its identifying claims (names, ABN, ACN,
                  postcode, state), all taken from the canonical observations the engine emits.
  source entity   records of ONE source that share an exact identifier: ABN, else ACN, else
                  normalised name + postcode. This collapses e.g. 900 invoices to one supplier.
                  It is deterministic (L1); no judgement is involved. Individuals licensed under
                  an employer's ABN are kept out of that employer's source entity.
  link            a scored, evidenced claim that two source entities from DIFFERENT sources are
                  the same business. Pairwise, because a pair is what a human can check and what
                  a future matcher can re-score. Every link has a stable id and carries the
                  features it was scored on, the matcher version and the extractor versions.
  entity          a connected component of accepted links, under a key derived from identifiers
                  (abn:<ABN>, acn:<ACN>, name:<hash>), never a sequence number, so a rerun or a
                  better matcher gives comparable keys. A merge that would put two different
                  ABNs or ACNs into one entity is refused.
  relationship    a typed edge between businesses (trustee_of). It never merges entities.

Linking reads every record of each source, not the 1,000-record extract: two random
1,000-row samples of different registers barely overlap, and that would measure the
sample, not the matcher.

Replacing the matcher: every scored pair is kept in candidates.jsonl, the previous version's links
are kept in history/, and `link-diff` reports what changed. See WRITEUP.md, "Change".
"""

import csv
import datetime as dt
import hashlib
import json
import math
import random
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

from . import CHECKS, CONFIGS, OUTPUTS, engine
from .names import best_sim, legal_form, name_sim, norm_name
from .review import is_approved, load_config, raw_status

MATCHER_VERSION = "rules-0.2.0"     # stamped on every link; bump it whenever a scoring rule changes
BLOCKING_VERSION = "blocks-0.1.0"   # a new matcher can only re-score pairs that blocking generated
THRESHOLD = 0.80                    # a pair scoring at or above this becomes a link
LOOSE_THRESHOLD = 0.60              # pairs from here up to THRESHOLD are sampled into the "looser band" hand-check
MAX_NAME_BLOCK = 4          # a normalised name held by more source entities than this in one source is "common"
NAME_BLOCK_CAP = 60         # pairs are only generated from the first this-many members of a name block
UNRELATED_NAME_SIM = 0.5    # names scoring below this are treated as different businesses
SAME_LEGAL_NAME_SIM = 0.8   # two legal names must score at least this to count as the same legal name
CHECK_N, CHECK_SEED = 50, 7 # 50 accepted links drawn for the hand-check, with a fixed seed
LOOSE_CHECK_N = 25          # 25 near-misses drawn from the looser band
LINK_DIR = OUTPUTS / "links"
VERDICT_COL = "same_business (y/n)"

# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------

# Words that mark a name as an organisation. A name containing any of them is not read as a person.
BUSINESS_WORDS = {
    "PTY", "LTD", "LIMITED", "INC", "INCORPORATED", "CORPORATION", "COMPANY", "TRUST", "TRUSTEE", "GROUP",
    "HOLDINGS", "SERVICES", "SERVICE", "SOLUTIONS", "CONSULTING", "CONSULTANTS", "AUSTRALIA", "AUSTRALIAN",
    "ENTERPRISES", "INDUSTRIES", "INTERNATIONAL", "CONSTRUCTIONS", "CONSTRUCTION", "BUILDING", "BUILDERS",
    "PLUMBING", "ELECTRICAL", "HOTEL", "CLUB", "BAR", "CAFE", "RESTAURANT", "TAVERN", "BOTTLESHOP", "LIQUOR",
    "CELLARS", "WINES", "BREWING", "FOUNDATION", "ASSOCIATION", "SOCIETY", "COUNCIL", "CHURCH", "SCHOOL",
    "COLLEGE", "UNIVERSITY", "HEALTH", "MEDICAL", "CARE", "SUPPLIES", "SUPPLY", "SYSTEMS", "TECHNOLOGIES",
    "TECHNOLOGY", "MANAGEMENT", "PARTNERS", "AND", "FINANCE", "FINANCIAL", "CAPITAL", "INVESTMENTS",
    "LENDING", "MORTGAGE", "MORTGAGES", "LOANS", "INSURANCE", "BROKERS", "PROPERTY", "REALTY", "MINISTRIES",
    "CENTRE", "CENTER", "NETWORK", "INSTITUTE", "DEPARTMENT", "STATE", "OF", "SHIRE", "CITY", "BANK",
    # Words that rarely sit in a person's name. Added after short organisation names were read as people
    # ("The Hub", "House With No Steps", "Transport Heritage NSW", "Scripture Union Queensland").
    "THE", "WITH", "FOR", "NSW", "VIC", "QLD", "QUEENSLAND", "VICTORIA", "TASMANIA", "NATIONAL", "REGIONAL",
    "COMMUNITY", "UNION", "HERITAGE", "TRANSPORT", "ARTS", "MUSIC", "THEATRE", "COMMITTEE", "BOARD", "AGENCY",
    "OFFICE", "MUSEUM", "GALLERY", "ACADEMY", "ALLIANCE", "PROJECT", "PROGRAM", "FUND", "HUB",
}
# Entity types that say nothing about being an organisation. Any other type (company, trust, ...)
# settles it: the record is not a person, whatever the name looks like.
ORG_TYPES_EXCLUDED = {"unknown", "sole_trader"}
# "<trustee> ATF <trust>" and its spelled-out forms. The two named groups capture each side.
_TRUSTEE = re.compile(r"^(?P<trustee>.+?)\s+(?:ATF|A\.T\.F\.?|AS TRUSTEE FOR|AS TRUSTEE OF)\s+(?P<trust>.+)$", re.I)


def looks_like_person(raw_names, entity_types):
    """A heuristic: 2-4 alphabetic tokens, none of them business-like, on a record not typed as an
    organisation. It still reads some short organisation names as people. That errs towards refusing
    a link, which is the safe direction, and the unlinked queue says so."""
    if set(entity_types) - ORG_TYPES_EXCLUDED:
        return False
    for n in raw_names:
        toks = re.sub(r"[^A-Z ]+", " ", n.upper()).split()
        if not (2 <= len(toks) <= 4) or BUSINESS_WORDS & set(toks):
            return False
    return bool(raw_names)


def link_id(uid_a, uid_b):
    """Stable across runs and matcher versions: the same pair always has the same id."""
    a, b = sorted((uid_a, uid_b))
    return hashlib.sha1(f"{a}|{b}".encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Records -> source entities
# --------------------------------------------------------------------------


def _record_claims(rec_obs):
    """Fold one record's observations back into a small dict of what identifies it. The linker
    reads only these canonical fields, never a source's own column names, which is why it needs no
    per-source code. `names` holds legal and trading names together; `legal` holds legal names only."""
    c = {"names": [], "legal": [], "abn": None, "acn": None, "postcode": None, "state": None, "locality": None,
         "full": None, "entity_type": None}
    for o in rec_obs:
        sect, field = o["field"].split(".", 1)
        if field == "legal_name":
            c["names"].append(o["value"])
            c["legal"].append(o["value"])
        elif field == "trading_name":
            c["names"].append(o["value"])
        elif field in ("abn", "acn", "postcode", "state", "locality", "full", "entity_type") and c[field] is None:
            c[field] = o["value"]
    return c


class SourceEntity:
    """All the records of ONE source that share an identifier: one business as that source sees it.
    Counters (names, postcodes, ...) record how often each value appears across those records, so
    the most common one can be shown. __slots__ fixes the attribute list, which saves memory when
    there are 200,000 of these objects."""
    __slots__ = ("source_id", "key", "abn", "acn", "names", "legal_names", "norms", "legal_norms", "forms",
                 "person_norms", "postcodes", "states", "localities", "entity_types", "records", "person_records",
                 "_person_names", "_person_legal", "_dirty")

    def __init__(self, source_id, key):
        self.source_id, self.key = source_id, key
        self.abn = self.acn = None
        self.names, self.legal_names, self.postcodes, self.states, self.localities = (
            Counter(), Counter(), Counter(), Counter(), Counter())
        self.norms, self.legal_norms, self.entity_types = set(), set(), set()
        self.forms = defaultdict(set)
        self.records, self.person_records = [], []
        self._person_names, self._person_legal = Counter(), Counter()
        self._dirty = None

    def add(self, rid, c):
        # Add one record. Its location and type always count. Its names go into one of two piles:
        # person-looking records are held apart (see finish), everything else is the business.
        self.abn = self.abn or c["abn"]
        self.acn = self.acn or c["acn"]
        for attr, f in (("postcodes", "postcode"), ("states", "state"), ("localities", "locality")):
            if c[f]:
                getattr(self, attr)[c[f]] += 1
        if c["entity_type"]:
            self.entity_types.add(c["entity_type"])
        if looks_like_person(c["names"], {c["entity_type"]} - {None}):
            self.person_records.append(rid)
            self._person_names.update(c["names"])
            self._person_legal.update(c["legal"])
        else:
            self.records.append(rid)
            self.names.update(c["names"])
            self.legal_names.update(c["legal"])

    def finish(self):
        """Occupational licences carry the employer's ABN, so one QBCC ABN can hold a university and
        fifty individual mechanics. Those people are not that business: their records stay out of its
        names, its link evidence and its profile. If every record is person-like, the entity is a person."""
        if not self.records:
            self.records, self.person_records = self.person_records, []
            self.names, self.legal_names = self._person_names, self._person_legal
        for n in self.names:
            nm = norm_name(n)
            if nm:
                self.norms.add(nm)
                form = legal_form(n)
                if form:
                    self.forms[nm].add(form)
        self.legal_norms = {norm_name(n) for n in self.legal_names} - {""}
        self.person_norms = {norm_name(n) for n in self._person_names} - {""} if self.person_records else set()

    @property
    def uid(self):
        return f"{self.source_id}::{self.key}"

    def display_name(self):
        return self.names.most_common(1)[0][0] if self.names else ""

    def dominant_norm(self):
        return norm_name(self.display_name())

    def is_business_like(self):
        return bool(self.abn or self.acn or "company" in self.entity_types
                    or any(BUSINESS_WORDS & set(n.upper().split()) for n in self.names))

    def is_person_like(self):
        return looks_like_person(list(self.names), self.entity_types)

    def carries_unrelated_names(self):
        """One identifier holding legal names that look like different businesses: the identifier is a
        checksum-valid number, but the pairing with these names is not reliable. Trading and premises
        names don't count, because those legitimately differ from the legal name."""
        if self._dirty is None:
            top = sorted({norm_name(n) for n, _ in self.legal_names.most_common(30)} - {""})
            self._dirty = bool(self.abn or self.acn) and any(
                (name_sim(x, y) or 0) < UNRELATED_NAME_SIM for i, x in enumerate(top) for y in top[i + 1:])
        return self._dirty

    def brief(self):
        return {"source_id": self.source_id, "source_entity": self.key,
                "source_record_id": self.records[0], "n_records": len(self.records),
                "name": self.display_name(), "abn": self.abn, "acn": self.acn,
                "postcode": self.postcodes.most_common(1)[0][0] if self.postcodes else None,
                "state": self.states.most_common(1)[0][0] if self.states else None}


def load_sources(allow_unapproved=False, limit=None):
    """Stream every eligible config over its full raw file. Returns (sources, source entities)."""
    sources, ses = [], {}
    for path in sorted(CONFIGS.glob("*.yaml")):
        slug = path.stem
        cfg = load_config(slug)
        status = (cfg.get("review") or {}).get("status")
        approved = is_approved(cfg)
        if not approved and not allow_unapproved:
            print(f"  {slug}: skipped (not approved)")
            continue
        errors, n = [], 0
        sid = cfg["source_id"]
        raw_file, raw = raw_status(cfg)
        # The same engine and the same approved config as `extract`, but over every row and
        # streamed: observations are grouped into source entities as they arrive, never all stored.
        for rid, rec_obs in engine.iter_records(cfg, raw_file, errors, limit=limit):
            n += 1
            c = _record_claims(rec_obs)
            nm = norm_name(c["names"][0]) if c["names"] else ""
            # The grouping key within a source, strongest identifier first: ABN, else ACN, else
            # normalised name + postcode. Records with the same key are the same source entity.
            if c["abn"]:
                key = f"abn:{c['abn']}"
            elif c["acn"]:
                key = f"acn:{c['acn']}"
            elif nm:
                key = f"name:{nm}|{c['postcode'] or ''}"
            else:
                continue  # nothing identifying; cannot be linked or queued meaningfully
            se = ses.get(f"{sid}::{key}")
            if se is None:
                se = ses[f"{sid}::{key}"] = SourceEntity(sid, key)
            se.add(rid, c)
        mine = [s for s in ses.values() if s.source_id == sid]
        for s in mine:
            s.finish()
        print(f"  {slug}: {n} records -> {len(mine)} source entities ({len(errors)} field errors)")
        sources.append({"slug": slug, "source_id": sid, "records": n, "source_entities": len(mine),
                        "review_status": status, "approved": approved,
                        "extractor_version": engine.extractor_version(cfg),
                        "source_reliability": (cfg.get("source_reliability") or {}).get("value"), **raw})
    return sources, ses


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def _score_name_tier(a, b, sim, pair, shared_pc, shared_state, name_counts):
    """Returns (confidence, refuse_reason, extra evidence) for a pair with no shared identifier."""
    # This reads as a list of reasons to refuse, most decisive first. Each `return` below is one
    # refusal with its own score and its own written reason; that reason is what appears in the
    # unlinked queue. Only a pair that survives all of them reaches the scoring at the bottom, and
    # there only an exact normalised name plus a shared postcode gets above the 0.80 threshold.
    if sim is None or sim < 0.9:
        return round((sim or 0) * 0.4, 3), "names not similar enough for a name-only link", {}
    if a.abn and b.abn:
        return 0.0, "same or similar name but different valid ABNs: different legal entities", {}
    if a.acn and b.acn:
        return 0.0, "same or similar name but different valid ACNs: different companies", {}
    if a.is_person_like() or b.is_person_like():
        return 0.40, ("looks like a person's name (heuristic, can misfire on short organisation names); "
                      "name-only links between people are refused"), {}
    via_legal = pair[0] in a.legal_norms and pair[1] in b.legal_norms
    extra = {"matched_via": "legal_names" if via_legal else "trading_or_premises_name"}
    common = max(name_counts[(a.source_id, pair[0])], name_counts[(b.source_id, pair[1])])
    if common > MAX_NAME_BLOCK:
        return 0.30, f"common name: {common} different businesses share it in one source", extra
    # An identifier-holding side whose names disagree: only its main name can speak for that identifier.
    for se, nm in ((a, pair[0]), (b, pair[1])):
        if se.carries_unrelated_names() and nm != se.dominant_norm():
            return 0.50, ("the matched name is a minor name on an identifier that carries unrelated names, "
                          "so it may not belong to that identifier"), extra
    # Both sides state a legal name and those disagree, so the match is only a premises or trading name.
    # A venue called "University of Newcastle" run by NUSERVICES PTY LTD is not the university.
    if not via_legal and a.legal_norms and b.legal_norms:
        legal_sim, _ = best_sim(a.legal_norms, b.legal_norms)
        extra["legal_name_sim"] = legal_sim
        if (legal_sim or 0) < SAME_LEGAL_NAME_SIM:
            return 0.55, "matched on a premises or trading name, but the two legal names differ", extra
    # norm_name removes legal forms so spellings compare equal, so check them here.
    forms_a, forms_b = a.forms.get(pair[0], set()), b.forms.get(pair[1], set())
    if forms_a and forms_b and not forms_a & forms_b:
        extra["legal_forms"] = [sorted(forms_a), sorted(forms_b)]
        return 0.60, "same name but different legal forms (e.g. Limited vs Incorporated): may be different entities", extra
    # The surviving pairs. Exact name + same postcode: 0.85 (legal names) or 0.82 (trading or
    # premises name). Exact name + same state only: 0.65. Exact name, no shared location: 0.45.
    # Near-identical name: 0.70 with a shared postcode, else 0.50. A one-word name loses 0.10,
    # because a single word is much more likely to belong to two different businesses.
    one_token = len(pair[0].split()) < 2
    if sim == 1.0 and shared_pc:
        conf = 0.85 if via_legal else 0.82
    elif sim == 1.0 and shared_state:
        conf = 0.65
    elif sim == 1.0:
        conf = 0.45
    else:
        conf = 0.70 if shared_pc else 0.50
    if one_token:
        conf -= 0.10
    refuse = None
    if conf < THRESHOLD:
        refuse = ("name matches but location does not corroborate it" if sim == 1.0
                  else "near-identical name without an identifier")
    return conf, refuse, extra


def score(a, b, name_counts):
    """Score one cross-source pair. Returns a candidate dict; decision is made by the caller."""
    sim, pair = best_sim(a.norms, b.norms)
    if a.abn and a.abn == b.abn and (a.person_norms or b.person_norms):
        # The person heuristic misreads short company names ("UBS AG", "BECTON DICKINSON"). Those rows
        # stay out of the entity, but a name the other register agrees with is still evidence for the ABN.
        sim, pair = best_sim(a.norms | a.person_norms, b.norms | b.person_norms)
    shared_pc = bool(set(a.postcodes) & set(b.postcodes))
    shared_state = bool(set(a.states) & set(b.states))
    ev = {"name_a": a.display_name(), "name_b": b.display_name(), "name_sim": sim,
          "names_compared": list(pair) if pair else None,
          "abn_a": a.abn, "abn_b": b.abn, "acn_a": a.acn, "acn_b": b.acn,
          "same_postcode": shared_pc, "same_state": shared_state,
          "unrelated_names_a": a.carries_unrelated_names(), "unrelated_names_b": b.carries_unrelated_names()}
    refuse = None
    # Four tiers, tried strongest first. The tier names the kind of evidence a link rests on.
    # Tier 1: both sides carry the same ABN. The names then adjust the score: agreeing names give
    # 0.99, partly agreeing 0.90, disagreeing 0.85, and no name to compare 0.95.
    if a.abn and b.abn and a.abn == b.abn:
        tier = "abn_exact"
        conf = 0.95 if sim is None else 0.99 if sim >= 0.8 else 0.90 if sim >= 0.5 else 0.85
        if (sim or 0) < UNRELATED_NAME_SIM and a.is_person_like() != b.is_person_like():
            # Seen in QBCC: occupational licences carry the employer's ABN, so individual mechanics
            # appear under the University of Queensland's ABN. Same ABN, different party.
            conf = 0.55
            refuse = ("ABN matches but one side names a person and the other an organisation "
                      "(e.g. an individual licensed under an employer's ABN)")
        elif (sim or 0) < UNRELATED_NAME_SIM and (a.carries_unrelated_names() or b.carries_unrelated_names()):
            # Seen in ACT invoices: one ABN against several unrelated supplier names. If no name agrees
            # with the other register either, the ABN on this row cannot be trusted to identify the party.
            conf = 0.60
            refuse = "ABN matches, but no name agrees and one side's ABN carries unrelated names in its own source"
    # Tier 2: the same ACN on both sides. Same idea, slightly lower bands.
    elif a.acn and b.acn and a.acn == b.acn:
        tier = "acn_exact"
        conf = 0.95 if sim is None else 0.98 if sim >= 0.8 else 0.88 if sim >= 0.5 else 0.82
    # Tier 3: one side has an ABN, the other an ACN, and the ABN's last 9 digits equal that ACN.
    elif (a.abn and b.acn and a.abn[2:] == b.acn) or (b.abn and a.acn and b.abn[2:] == a.acn):
        # The ontology: "the last 9 digits of an ABN are often the ACN, but not always". So the bridge
        # needs the names to agree as well.
        tier = "abn_acn_bridge"
        conf = 0.93 if (sim or 0) >= 0.8 else 0.60 if (sim or 0) >= 0.5 else 0.30
        if conf < THRESHOLD:
            refuse = "ABN suffix equals the other record's ACN, but the names disagree"
    # Tier 4: no identifier in common, so names and location are all there is. The weakest tier.
    else:
        tier = "name"
        conf, refuse, extra = _score_name_tier(a, b, sim, pair, shared_pc, shared_state, name_counts)
        ev.update(extra, name_only=True)
        ev["common_name_count"] = (max(name_counts[(a.source_id, pair[0])], name_counts[(b.source_id, pair[1])])
                                   if pair else None)
    return {"link_id": link_id(a.uid, b.uid), "a": a.uid, "b": b.uid, "tier": tier,
            "confidence": round(conf, 3), "refuse_reason": refuse, "evidence": ev}


def candidates(ses, stats=None):
    """Blocking: only pairs from different sources that share an ABN, ACN, ABN-suffix/ACN or name key.
    Everything is sorted, so the same input gives the same pairs in the same order on every run."""
    # Comparing every source entity with every other would be about 200,000 x 200,000 / 2 = 20
    # billion pairs. Blocking avoids that: each entity is filed under a few keys (its ABN, its ACN,
    # each of its names), and only entities filed under the same key are ever compared. The cost is
    # that a true match sharing none of these keys is never seen at all.
    by = defaultdict(list)
    for uid in sorted(ses):
        se = ses[uid]
        # An ABN is filed twice: whole, and by its last 9 digits under "bridge", where it can meet
        # another source's ACN.
        if se.abn:
            by[("abn", se.abn)].append(se)
            by[("bridge", se.abn[2:])].append(se)
        if se.acn:
            by[("acn", se.acn)].append(se)
            by[("bridge", se.acn)].append(se)
        # A name is filed twice as well: as written, and with its words sorted, so "GOYAL PANKAJ"
        # and "PANKAJ GOYAL" land in the same block.
        for nm in sorted(se.norms):
            by[("name", nm)].append(se)
            by[("name_sorted", " ".join(sorted(nm.split())))].append(se)
    # `seen` stops a pair being yielded twice when two entities share more than one key.
    seen = set()
    for key, group in by.items():
        # A block whose members all come from one source can produce no cross-source pair.
        if len({s.source_id for s in group}) < 2:
            continue
        if key[0].startswith("name") and len(group) > NAME_BLOCK_CAP:
            if stats is not None:
                stats.setdefault("name_blocks_truncated", []).append({"name": key[1], "members": len(group)})
            group = group[:NAME_BLOCK_CAP]  # pathological name blocks; their pairs score as common names anyway
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if a.source_id == b.source_id:
                    continue
                pair = tuple(sorted((a.uid, b.uid)))
                if pair not in seen:
                    seen.add(pair)
                    yield (a, b) if a.uid == pair[0] else (b, a)


# --------------------------------------------------------------------------
# Clustering
# --------------------------------------------------------------------------


class Clusters:
    """Union-find (disjoint sets): the standard structure for "which things are connected".
    Every source entity starts as its own group. Accepting a link joins two groups. `parent` points
    each member towards its group's representative (the root); two members are in the same entity
    exactly when find() returns the same root for both. Each root also remembers which ABNs and
    ACNs its group holds, which is what lets union() refuse a merge that would mix two of them."""

    def __init__(self):
        self.parent, self.abns, self.acns = {}, {}, {}

    def find(self, x):
        # Follow parent pointers up to the root. The line inside the loop is path halving: it
        # re-points each visited member to its grandparent, which keeps later lookups short.
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b, ses=None):
        # Join the groups of a and b. Returns None on success, or the reason the merge was refused.
        ra, rb = self.find(a.uid), self.find(b.uid)
        if ra == rb:
            return None  # already in the same entity: this link only confirms it
        for r, se in ((ra, a), (rb, b)):
            self.abns.setdefault(r, {se.abn} - {None})
            self.acns.setdefault(r, {se.acn} - {None})
        # The safety rule. A links to B and B links to C can each look fine alone and still chain
        # two different businesses into one entity. If both groups already hold an ABN (or ACN) and
        # they differ, the merge is refused however good this one link looks.
        if self.abns[ra] and self.abns[rb] and self.abns[ra] != self.abns[rb]:
            return f"would merge two entities with different ABNs {sorted(self.abns[ra] | self.abns[rb])}"
        if self.acns[ra] and self.acns[rb] and self.acns[ra] != self.acns[rb]:
            return f"would merge two entities with different ACNs {sorted(self.acns[ra] | self.acns[rb])}"
        self.parent[rb] = ra
        self.abns[ra] |= self.abns.pop(rb)
        self.acns[ra] |= self.acns.pop(rb)
        return None


def entity_key(members):
    """The canonical_entity_key, built from what the entity is: its ABN, else its ACN, else a hash
    of its first member. A counter (entity 1, 2, 3) would hand out different numbers on every run;
    this gives the same business the same key on a rerun and under a different matcher, and that is
    what makes two runs comparable (see diff_links)."""
    abns = sorted({m.abn for m in members if m.abn})
    if abns:
        return f"abn:{abns[0]}"
    acns = sorted({m.acn for m in members if m.acn})
    if acns:
        return f"acn:{acns[0]}"
    anchor = sorted(m.uid for m in members)[0]
    return "name:" + hashlib.sha1(anchor.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------
# Relationships
# --------------------------------------------------------------------------


def relationships(ses, se_entity):
    """Find "X ATF Y" names (X as trustee for Y) and record each as a trustee_of edge from X to Y.
    The trust's own entity is known: it is the record the name was found on. The trustee is only
    resolved to an entity when exactly one source entity carries that name; otherwise the edge
    keeps the trustee's name as text and says it is unresolved."""
    by_norm = defaultdict(set)
    for se in ses.values():
        for nm in se.norms:
            by_norm[nm].add(se.uid)
    out = []
    for se in ses.values():
        for raw in se.names:
            m = _TRUSTEE.match(raw.strip())
            if not m:
                continue
            # Resolve the trustee only when exactly one source entity anywhere carries that name.
            hits = by_norm.get(norm_name(m.group("trustee")), set()) - {se.uid}
            trustee = next(iter(hits)) if len(hits) == 1 else None
            out.append({
                "kind": "trustee_of",
                "from_name": m.group("trustee").strip(),
                "from_source_entity": trustee,
                "from_entity": se_entity.get(trustee) if trustee else None,
                "from_resolution": "unique exact name match" if trustee else
                f"unresolved ({len(hits)} source entities carry that name)",
                "to_name": m.group("trust").strip(),
                "to_entity": se_entity.get(se.uid),
                "to_source_entity": se.uid,
                "evidence": f"name pattern '<trustee> ATF <trust>' in {raw!r}",
                "derivation_level": "L1",
                "confidence": 0.9,
                "note": "The ABN belongs to the trust; the trustee is a separate legal entity. Kept as an edge, not merged.",
            })
    return out


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------


def _jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")


def _read_jsonl(path):
    return [json.loads(l) for l in open(path, encoding="utf-8")]


def _archive_previous_links():
    """A new matcher version must not overwrite the links it replaces: they are the baseline for the diff."""
    path = LINK_DIR / "links.jsonl"
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        first = f.readline()
    old = json.loads(first).get("matcher_version") if first.strip() else None
    if old and old != MATCHER_VERSION:
        hist = LINK_DIR / "history"
        hist.mkdir(exist_ok=True)
        shutil.copyfile(path, hist / f"links.{old}.jsonl")
        print(f"  kept the {old} links in {hist.name}/links.{old}.jsonl")


def run(allow_unapproved=False, threshold=THRESHOLD, limit=None):
    LINK_DIR.mkdir(parents=True, exist_ok=True)
    _archive_previous_links()
    created_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    print("reading sources")
    sources, ses = load_sources(allow_unapproved, limit)
    if not ses:
        raise SystemExit("no source could be read: no approved configs. Approve them, or pass --allow-unapproved.")
    extractor = {s["source_id"]: s["extractor_version"] for s in sources}
    # How many source entities in each source carry each normalised name. A name held by many
    # different businesses in one source ("THE ROYAL HOTEL") is too common to link on.
    name_counts = Counter((se.source_id, nm) for se in ses.values() for nm in se.norms)

    print("scoring candidate pairs")
    block_stats = {}
    cands = [score(a, b, name_counts) for a, b in candidates(ses, block_stats)]
    cands.sort(key=lambda c: (-c["confidence"], c["link_id"]))

    # Accept links strongest first (cands is sorted by confidence, descending). The order matters:
    # the most trusted links build the entities, so when two links would pull different ABNs
    # together, it is the weaker one that arrives later and is refused.
    cl = Clusters()
    accepted, refused_merge = [], []
    for c in cands:
        if c["confidence"] < threshold:
            continue
        reason = cl.union(ses[c["a"]], ses[c["b"]])
        if reason:
            c["refuse_reason"] = reason
            refused_merge.append(c)
        else:
            accepted.append(c)
    accepted_ids = {c["link_id"] for c in accepted}

    # Entities = the groups the accepted links formed. Collect each group's members under its root.
    # A source entity with no accepted link is in no group, so it gets no entity key at all.
    groups = defaultdict(list)
    for c in accepted:
        for u in (c["a"], c["b"]):
            groups[cl.find(u)].append(u)
    se_entity, entities = {}, []
    for root, uids in groups.items():
        members = [ses[u] for u in dict.fromkeys(uids)]
        key = entity_key(members)
        for m in members:
            se_entity[m.uid] = key
        entities.append({"canonical_entity_key": key, "sources": sorted({m.source_id for m in members}),
                         "n_sources": len({m.source_id for m in members}),
                         "members": [m.brief() for m in sorted(members, key=lambda m: m.uid)]})
    entities.sort(key=lambda e: (-e["n_sources"], e["canonical_entity_key"]))

    # Membership: every record of every linked source entity, with its link confidence
    # (the best accepted link that attached its source entity to the entity).
    best_link = {}
    for c in accepted:
        for u in (c["a"], c["b"]):
            if u not in best_link or c["confidence"] > best_link[u]["confidence"]:
                best_link[u] = c
    memberships = [{"canonical_entity_key": se_entity[u], "source_id": ses[u].source_id,
                    "source_record_id": rid, "source_entity": ses[u].key,
                    "link_confidence": best_link[u]["confidence"], "via_link_id": best_link[u]["link_id"],
                    "matcher_version": MATCHER_VERSION}
                   for u in sorted(se_entity) for rid in ses[u].records]
    individuals_excluded = sum(len(ses[u].person_records) for u in se_entity)

    # The deliverable rows: {source_a_record, source_b_record, canonical_entity_key, confidence,
    # evidence}, plus what is needed to re-check or replace a link later (versions, threshold, time).
    links = []
    for c in accepted:
        a, b = ses[c["a"]], ses[c["b"]]
        links.append({"link_id": c["link_id"], "source_a_record": a.brief(), "source_b_record": b.brief(),
                      "canonical_entity_key": se_entity[c["a"]], "confidence": c["confidence"],
                      "tier": c["tier"], "evidence": c["evidence"],
                      "matcher_version": MATCHER_VERSION, "blocking_version": BLOCKING_VERSION,
                      "threshold": threshold, "created_at": created_at,
                      "extractor_versions": {a.source_id: extractor[a.source_id], b.source_id: extractor[b.source_id]}})

    # Unlinked queue: business-like source entities that had at least one candidate and were never
    # attached at the threshold, with the strongest candidate we turned down and why.
    best_refused = {}
    for c in cands:
        if c["link_id"] in accepted_ids:
            continue
        for u, other in ((c["a"], c["b"]), (c["b"], c["a"])):
            if u in se_entity:
                continue
            if u not in best_refused or c["confidence"] > best_refused[u][0]["confidence"]:
                best_refused[u] = (c, other)
    queue = []
    for u, (c, other) in best_refused.items():
        se = ses[u]
        if not se.is_business_like() and not se.is_person_like():
            continue
        if (c["evidence"]["name_sim"] or 0) < 0.9 and c["tier"] == "name":
            continue  # a blocking collision (e.g. shared ABN suffix) with unrelated names: noise, not a decision
        queue.append({**se.brief(), "reason": c.get("refuse_reason") or f"best candidate scored {c['confidence']} < {threshold}",
                      "best_candidate": ses[other].brief(), "best_candidate_confidence": c["confidence"],
                      "link_id": c["link_id"], "tier": c["tier"], "evidence": c["evidence"]})
    queue.sort(key=lambda q: (-q["best_candidate_confidence"], q["source_id"], q["source_entity"]))

    rels = relationships(ses, se_entity)

    _jsonl(LINK_DIR / "links.jsonl", links)
    _jsonl(LINK_DIR / "entities.jsonl", entities)
    _jsonl(LINK_DIR / "memberships.jsonl", memberships)
    _jsonl(LINK_DIR / "unlinked_queue.jsonl", queue)
    _jsonl(LINK_DIR / "relationships.jsonl", rels)
    # Every pair the matcher scored, accepted or not, at any confidence. A future matcher re-scores these.
    _jsonl(LINK_DIR / "candidates.jsonl",
           [{**c, "accepted": c["link_id"] in accepted_ids, "matcher_version": MATCHER_VERSION,
             "blocking_version": BLOCKING_VERSION, "a_brief": ses[c["a"]].brief(), "b_brief": ses[c["b"]].brief()}
            for c in cands])
    stale = LINK_DIR / "candidates_below_threshold.jsonl"
    if stale.exists():
        stale.unlink()  # replaced by candidates.jsonl, which also keeps pairs below 0.3

    summary = {
        "matcher_version": MATCHER_VERSION, "blocking_version": BLOCKING_VERSION,
        "threshold": threshold, "loose_threshold": LOOSE_THRESHOLD, "created_at": created_at,
        "sources": sources,
        "all_configs_approved": all(s["approved"] for s in sources),
        "any_source_drifted": any(s["source_drifted"] for s in sources),
        "source_entities": len(ses),
        "candidate_pairs": len(cands),
        "candidate_pairs_by_tier": dict(Counter(c["tier"] for c in cands)),
        "accepted_links": len(accepted),
        "accepted_by_tier": dict(Counter(c["tier"] for c in accepted)),
        "accepted_by_confidence": {str(k): v for k, v in sorted(Counter(c["confidence"] for c in accepted).items())},
        "links_at_loose_threshold": sum(c["confidence"] >= LOOSE_THRESHOLD for c in cands),
        "refused_merges_conflicting_identifier": len(refused_merge),
        "entities": len(entities),
        "entities_by_n_sources": dict(Counter(e["n_sources"] for e in entities)),
        "links_by_source_pair": dict(Counter(" x ".join(sorted((l["source_a_record"]["source_id"],
                                                                 l["source_b_record"]["source_id"]))) for l in links)),
        "individual_records_excluded_from_employer_entities": individuals_excluded,
        "name_blocks_truncated": block_stats.get("name_blocks_truncated", []),
        "unlinked_queue": len(queue),
        "unlinked_by_reason": dict(Counter(q["reason"].split(":")[0].split(" (heuristic")[0] for q in queue).most_common(12)),
        "relationships": len(rels),
    }
    (LINK_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _write_checks(links, cands, ses, threshold)
    print(json.dumps({k: v for k, v in summary.items() if k != "sources"}, indent=2))
    return summary


# --------------------------------------------------------------------------
# Hand-check sheets and precision
# --------------------------------------------------------------------------

CHECK_COLS = ["link_no", "link_id", "confidence", "tier", "name_a", "source_a", "record_a", "abn_a", "acn_a",
              "postcode_a", "name_b", "source_b", "record_b", "abn_b", "acn_b", "postcode_b", "name_sim",
              "names_compared", VERDICT_COL, "notes"]


def _check_row(i, c, a, b):
    return [i, c["link_id"], c["confidence"], c["tier"], a["name"], a["source_id"], a["source_record_id"], a["abn"],
            a["acn"], a["postcode"], b["name"], b["source_id"], b["source_record_id"], b["abn"], b["acn"], b["postcode"],
            c["evidence"].get("name_sim"), " <> ".join(c["evidence"].get("names_compared") or []), "", ""]


def read_sheet(path):
    """Spreadsheet programs on Windows save CSV as UTF-8 with a BOM or as cp1252. Accept both."""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("latin-1")
    import io
    return list(csv.DictReader(io.StringIO(text, newline="")))


def _sheet_has_answers(path):
    return any(r.get(VERDICT_COL, "").strip() for r in read_sheet(path))


def _write_sheet(path, rows):
    """A sheet someone has started filling in is never overwritten. A blank one follows the current links."""
    if path.exists() and _sheet_has_answers(path):
        print(f"  {path.name} has answers, not overwritten; link-report flags any row that no longer matches")
        return
    CHECKS.mkdir(exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(CHECK_COLS)
        w.writerows(rows)
    print(f"  wrote {path.relative_to(path.parents[1])} ({len(rows)} rows to hand-check)")


def _write_checks(links, cands, ses, threshold):
    ordered = sorted(links, key=lambda l: l["link_id"])
    sample = random.Random(CHECK_SEED).sample(ordered, min(CHECK_N, len(ordered)))
    _write_sheet(CHECKS / "links_reviewed.csv",
                 [_check_row(i + 1, l, l["source_a_record"], l["source_b_record"]) for i, l in enumerate(sample)])
    loose = sorted((c for c in cands if LOOSE_THRESHOLD <= c["confidence"] < threshold), key=lambda c: c["link_id"])
    loose = random.Random(CHECK_SEED).sample(loose, min(LOOSE_CHECK_N, len(loose)))
    _write_sheet(CHECKS / "links_loose_reviewed.csv",
                 [_check_row(i + 1, c, ses[c["a"]].brief(), ses[c["b"]].brief()) for i, c in enumerate(loose)])


def wilson_interval(k, n, z=1.96):
    """95% interval for a proportion. With 50 checks a point estimate alone overstates what we know."""
    # k correct out of n checked. The Wilson score interval is used instead of the textbook
    # p +/- 1.96 * sqrt(p(1-p)/n) because that one collapses to zero width when p is 0 or 1:
    # 50 correct out of 50 would claim certainty. Wilson gives about [0.93, 1.0] for 50 of 50.
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [round((c - r) / d, 3), round((c + r) / d, 3)]


def report(reviewer=None):
    """Precision from the filled-in sheets: correct / checked, overall, per tier and per confidence
    band, each with a 95% interval. Every verdict is also saved to decisions.jsonl against its
    link_id and matcher version, so it can be replayed as a test when the matcher changes."""
    out = {}
    current = {r["link_id"]: r for r in _read_jsonl(LINK_DIR / "candidates.jsonl")} \
        if (LINK_DIR / "candidates.jsonl").exists() else {}
    decisions_path = LINK_DIR / "decisions.jsonl"
    decisions = {}
    if decisions_path.exists():
        for d in _read_jsonl(decisions_path):
            decisions[(d["sheet"], d["link_id"])] = d
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for name in ("links_reviewed", "links_loose_reviewed"):
        path = CHECKS / f"{name}.csv"
        if not path.exists():
            continue
        rows = read_sheet(path)
        done, stale = [], []
        for r in rows:
            a = r.get(VERDICT_COL, "").strip().lower()[:1]
            if a not in ("y", "n"):
                continue
            cur = current.get(r.get("link_id"))
            # A verdict is about one pair at one score from one matcher. If the link moved, it no longer measures it.
            if current and (cur is None or abs(float(r["confidence"]) - cur["confidence"]) > 1e-9):
                stale.append(r.get("link_no"))
                continue
            done.append((r, a))
        if not done:
            print(f"{name}: not filled in yet ({len(rows)} rows)" + (f"; {len(stale)} stale" if stale else ""))
            continue
        yes = sum(a == "y" for _, a in done)
        by_tier, by_conf = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
        for r, a in done:
            for bucket, k in ((by_tier, r["tier"]), (by_conf, r["confidence"])):
                bucket[k][0] += a == "y"
                bucket[k][1] += 1
            decisions.setdefault((name, r["link_id"]), {"sheet": name, "link_id": r["link_id"], "first_decided_at": now})
            decisions[(name, r["link_id"])].update(verdict=a, tier=r["tier"], confidence=float(r["confidence"]),
                                                   matcher_version=(current.get(r["link_id"]) or {}).get("matcher_version"),
                                                   reviewer=reviewer, notes=r.get("notes") or None)
        out[name] = {"checked": len(done), "of": len(rows), "correct": yes,
                     "precision": round(yes / len(done), 3),
                     "ci95": wilson_interval(yes, len(done)),
                     "stale_rows_ignored": stale,
                     "by_tier": {t: {"correct": c, "checked": n, "ci95": wilson_interval(c, n)}
                                 for t, (c, n) in sorted(by_tier.items())},
                     "by_confidence": {k: f"{c}/{n}" for k, (c, n) in sorted(by_conf.items())}}
    print(json.dumps(out, indent=2))
    if out:
        (LINK_DIR / "precision.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        _jsonl(decisions_path, sorted(decisions.values(), key=lambda d: (d["sheet"], d["link_id"])))
    return out


# --------------------------------------------------------------------------
# Comparing two matcher versions
# --------------------------------------------------------------------------


def _uid(rec):
    return f"{rec['source_id']}::{rec['source_entity']}"


def _index_links(path):
    out = {}
    for l in _read_jsonl(path):
        lid = l.get("link_id") or link_id(_uid(l["source_a_record"]), _uid(l["source_b_record"]))
        out[lid] = l
    return out


def _diff_row(l, why=None):
    a, b = l["source_a_record"], l["source_b_record"]
    row = {"link_id": l.get("link_id") or link_id(_uid(a), _uid(b)), "tier": l["tier"], "confidence": l["confidence"],
           "name_a": a["name"], "name_b": b["name"], "abn_a": a["abn"], "abn_b": b["abn"],
           "key": l["canonical_entity_key"]}
    if why:
        row["why"] = why
    return row


def diff_links(old_path, new_path=None, out_path=None):
    """What a matcher change did, link by link. This is the review queue for promoting a new matcher:
    pairs both versions accept need little attention, and every disagreement gets looked at."""
    new_path = Path(new_path) if new_path else LINK_DIR / "links.jsonl"
    old, new = _index_links(old_path), _index_links(new_path)
    cands = {c["link_id"]: c for c in _read_jsonl(LINK_DIR / "candidates.jsonl")} \
        if (LINK_DIR / "candidates.jsonl").exists() else {}
    removed, added, moved, same = [], [], [], 0
    for lid in sorted(set(old) | set(new)):
        o, n = old.get(lid), new.get(lid)
        if o and not n:
            cand = cands.get(lid)
            removed.append(_diff_row(o, (cand or {}).get("refuse_reason") or "pair no longer generated or scored"))
        elif n and not o:
            added.append(_diff_row(n))
        elif abs(o["confidence"] - n["confidence"]) > 1e-9 or o["tier"] != n["tier"]:
            moved.append({**_diff_row(n), "old_confidence": o["confidence"], "old_tier": o["tier"]})
        else:
            same += 1
    keys = lambda d: {l["canonical_entity_key"] for l in d.values()}
    result = {
        "old": str(Path(old_path).name), "new": str(new_path.name),
        "old_matcher": next(iter(old.values()), {}).get("matcher_version"),
        "new_matcher": next(iter(new.values()), {}).get("matcher_version"),
        "links_old": len(old), "links_new": len(new), "unchanged": same,
        "removed": len(removed), "added": len(added), "confidence_or_tier_changed": len(moved),
        "entities_old": len(keys(old)), "entities_new": len(keys(new)),
        "entities_only_in_old": len(keys(old) - keys(new)), "entities_only_in_new": len(keys(new) - keys(old)),
        "removed_by_tier": dict(Counter(r["tier"] for r in removed)),
        "added_by_tier": dict(Counter(r["tier"] for r in added)),
        "removed_links": removed, "added_links": added, "changed_links": moved,
    }
    out_path = Path(out_path) if out_path else LINK_DIR / f"diff_{result['old_matcher']}_to_{result['new_matcher']}.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if not k.endswith("_links")}, indent=2))
    print(f"wrote {out_path}")
    return result
