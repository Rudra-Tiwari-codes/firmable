"""Part 4: one merged profile per linked business, with per-field confidence and provenance.

Resolution rules (the same for every field, so a reviewer can predict them):

  weight of one observation   w = field_confidence x source_reliability x link_confidence
  support of a value          noisy-OR over SOURCES: 1 - prod(1 - w_s), where w_s is the best
                              observation of that value in source s. Collapsing per source means
                              900 invoices from one register count once, not 900 times.
  winner                      highest support; ties go to the newer observed_at, then to the
                              more reliable source.
  field confidence            support(winner) x agreement, where agreement =
                              support(winner) / sum of all values' support. Unanimous sources give
                              agreement 1; a contested field is visibly less certain.

The field confidence has to be one number a reader can sort on, but it mixes different kinds of
uncertainty, so each field also carries `confidence_components`: the mapping, source and link
confidences of its strongest observation, plus support and agreement, each kept separate.

Values that differ only in case, punctuation, legal-form spelling, word order or spacing ("Oz Harvest
Limited" / "OZ HARVEST LTD", "GOYAL, PANKAJ" / "PANKAJ GOYAL") are one value, and the spellings that
were merged are listed. The display form comes from the most reliable source.
`entity_type = unknown` is an abstention and does not compete with a real type.

The address is resolved as a whole: the winner is one source record's address, so a profile
never pairs a postcode from one record with a locality from another. Other addresses are
listed as alternatives, not as losers, because a business can have several real addresses.

Fields with no observation are absent from the profile.
"""

import datetime as dt
import json
from collections import defaultdict

from . import CONFIGS, OUTPUTS, engine
from .link import LINK_DIR, MATCHER_VERSION
from .names import norm_name, same_name_variant
from .review import is_approved, load_config, raw_path

PROFILE_VERSION = "profile-0.2.0"
PROFILE_DIR = OUTPUTS / "profiles"
# Scalar fields have one winning value per profile. Trading names (many allowed) and the address
# (resolved as a whole) are handled by their own functions below.
SCALAR_FIELDS = ["legal_name", "abn", "acn", "entity_type", "status", "website", "industry_code", "date_registered"]
ADDRESS_FIELDS = ["full", "locality", "state", "postcode", "country"]
NAME_FIELDS = ("legal_name", "trading_name")
MAX_PROVENANCE = 5   # list at most 5 source records per value; n_records still gives the full count
# The date a claim's valid_to is judged against. Set once per run and written into every profile,
# so a profile can be reproduced and "why did this change next month" has an answer.
AS_OF = None


def _jsonl(path):
    return [json.loads(l) for l in open(path, encoding="utf-8")]


def select_entities(n):
    """Deterministic: most sources first, then round-robin across source combinations so the 50
    are not all from the biggest overlap."""
    ents = _jsonl(LINK_DIR / "entities.jsonl")
    by_combo = defaultdict(list)
    for e in ents:
        by_combo[(e["n_sources"], tuple(e["sources"]))].append(e)
    combos = sorted(by_combo, key=lambda c: (-c[0], -len(by_combo[c]), c[1]))
    picked = []
    while len(picked) < n and any(by_combo[c] for c in combos):
        for c in combos:
            if by_combo[c] and len(picked) < n:
                picked.append(by_combo[c].pop(0))
    return picked


def collect_observations(keys, allow_unapproved):
    """Re-stream each source and keep the observations of member records of the chosen entities."""
    # memberships.jsonl says which source records belong to which entity. `want` turns that into a
    # lookup: (source, record id) -> membership row. Each source is then streamed once through the
    # engine and only the wanted records' observations are kept, each tagged with the confidence
    # of the link that attached its record to the entity.
    members = _jsonl(LINK_DIR / "memberships.jsonl")
    want = {}
    for m in members:
        if m["canonical_entity_key"] in keys:
            want[(m["source_id"], m["source_record_id"])] = m
    by_source = {sid for sid, _ in want}
    obs = defaultdict(list)
    for path in sorted(CONFIGS.glob("*.yaml")):
        cfg = load_config(path.stem)
        if cfg["source_id"] not in by_source:
            continue
        if not is_approved(cfg) and not allow_unapproved:
            continue
        for rid, rec_obs in engine.iter_records(cfg, raw_path(cfg), []):
            m = want.get((cfg["source_id"], rid))
            if m:
                for o in rec_obs:
                    obs[m["canonical_entity_key"]].append({**o, "link_confidence": m["link_confidence"]})
    return obs


def _weight(o):
    """How much one observation counts, from 0 to 1: the product of the three confidences the
    ontology keeps apart. Is the mapping right x is the publisher reliable x is this record really
    this entity. A product, because the claim is only as good as all three together, and a missing
    confidence counts as 0."""
    w = (o.get("field_confidence") or 0) * (o.get("source_reliability") or 0) * (o.get("link_confidence") or 0)
    if o.get("valid_to") and str(o["valid_to"])[:10] < (AS_OF or dt.date.today().isoformat()):
        w *= 0.5  # the source itself says this claim has ended
    return w


def _prov(o):
    # Provenance for one observation: enough to find the raw record and to see how it was weighted.
    return {k: o.get(k) for k in ("source_id", "source_record_id", "raw_value", "source_field", "observed_at",
                                  "valid_to", "field_confidence", "source_reliability", "link_confidence",
                                  "derivation_level", "extractor_version")}


def _value_key(field, v):
    # Two observations state "the same value" when their keys match. Names compare after
    # normalising; everything else compares as trimmed lower-case text.
    return norm_name(v) if field in NAME_FIELDS else str(v).strip().lower()


def _merge_name_variants(groups):
    """Fold spellings of one name together, so 'GOYAL, PANKAJ' and 'PANKAJ GOYAL' do not read as a
    disagreement. The largest group's key survives; the order is fixed so a rerun gives the same result."""
    merged = {}
    for key in sorted(groups, key=lambda k: (-len(groups[k]), k)):
        home = next((m for m in merged if key and same_name_variant(key, m)), None)
        if home is None:
            merged[key] = list(groups[key])
        else:
            merged[home].extend(groups[key])
    return merged


def _support(observations):
    """Noisy-OR over sources, using each source's best observation."""
    # Noisy-OR treats each source as an independent witness that is right with probability w.
    # The value is unsupported only if every witness is wrong, which has probability
    # (1 - w1) x (1 - w2) x ..., so support = 1 minus that. Two sources at 0.7 give
    # 1 - 0.3 x 0.3 = 0.91: agreement between sources raises support, and it can never pass 1.
    best = {}
    for o in observations:
        if o["source_id"] not in best or _weight(o) > _weight(best[o["source_id"]]):
            best[o["source_id"]] = o
    p = 1.0
    for o in best.values():
        p *= 1 - _weight(o)
    return 1 - p, best


def _group(field, observations):
    """Gather the observations of one field into groups that state the same value, score each
    group's support, and return the groups best first. groups[0] is the winner; the rest lost."""
    groups = defaultdict(list)
    for o in observations:
        groups[_value_key(field, o["value"])].append(o)
    if field in NAME_FIELDS:
        groups = _merge_name_variants(groups)
    out = []
    for key, os_ in groups.items():
        sup, best = _support(os_)
        display = max(os_, key=lambda o: (o.get("source_reliability") or 0, o.get("field_confidence") or 0))["value"]
        out.append({"key": key, "value": display, "support": round(sup, 4), "observations": os_, "best": best,
                    "newest": max(str(o.get("observed_at") or "") for o in os_),
                    "max_reliability": max(o.get("source_reliability") or 0 for o in os_)})
    # support desc; ties -> newest observed_at first, then more reliable
    out.sort(key=lambda g: (g["support"], g["newest"], g["max_reliability"]), reverse=True)
    return out


def _why_lost(winner, loser):
    if loser["support"] < winner["support"]:
        ws, ls = sorted(winner["best"]), sorted(loser["best"])
        return (f"lower support {loser['support']} vs {winner['support']} "
                f"({len(ls)} source(s) {ls} vs {len(ws)} source(s) {ws})")
    if loser["newest"] < winner["newest"]:
        return f"tied on support; winner observed more recently ({winner['newest']} vs {loser['newest']})"
    return "tied on support and recency; winner's source is more reliable"


def _provenance_list(g):
    obs = sorted(g["observations"], key=_weight, reverse=True)
    return {"records": [_prov(o) for o in obs[:MAX_PROVENANCE]], "n_records": len(obs),
            "sources": sorted(g["best"])}


def resolve_scalar(field, observations):
    if field == "entity_type":
        # unknown is an abstention, not a value: it never competes, and an all-unknown field is absent.
        observations = [o for o in observations if o["value"] != "unknown"]
    groups = _group(field, observations)
    if not groups:
        return None
    w = groups[0]
    # Agreement = the winner's share of all support. With no competing value it is 1. A winner at
    # 0.9 against a loser at 0.6 has agreement 0.9 / 1.5 = 0.6, so its confidence is 0.9 x 0.6 =
    # 0.54: a contested field is reported as less certain than the same value uncontested.
    total = sum(g["support"] for g in groups)
    agreement = w["support"] / total if total else 0
    top = max(w["observations"], key=_weight)
    spellings = sorted({str(o["value"]) for o in w["observations"]})
    return {
        "value": w["value"],
        "confidence": round(w["support"] * agreement, 3),
        # The one number above mixes several kinds of uncertainty, so the parts are kept visible.
        "confidence_components": {"mapping": top.get("field_confidence"), "source_reliability": top.get("source_reliability"),
                                  "link": top.get("link_confidence"), "support": w["support"], "agreement": round(agreement, 3)},
        **({"spellings_merged": spellings} if len(spellings) > 1 else {}),
        "provenance": _provenance_list(w),
        "decision": {"support": w["support"], "agreement": round(agreement, 3),
                     "rule": "highest noisy-OR support across sources; ties to newest, then most reliable"
                     if len(groups) > 1 else "uncontested: every source that states this field agrees"},
        **({"disagreements": [{"value": g["value"], "support": g["support"], "why_lost": _why_lost(w, g),
                               "provenance": _provenance_list(g)} for g in groups[1:]]} if len(groups) > 1 else {}),
    }


def resolve_trading_names(observations, legal_key):
    # A business can have several trading names, so there is no contest here: every distinct name
    # is kept with its own support. A trading name that is only a spelling of the legal name is
    # dropped, because it adds nothing.
    out = []
    for g in _group("trading_name", observations):
        if not g["key"] or (legal_key and same_name_variant(g["key"], legal_key)):
            continue
        out.append({"value": g["value"], "confidence": round(g["support"], 3), "provenance": _provenance_list(g)})
    return out  # multi-valued: no winner, each name stands on its own support


def resolve_address(observations):
    # 1. Put each record's address parts back together (one record = one address).
    # 2. Group the records by (state, postcode): records in the same group are the same place.
    # 3. Score each group by noisy-OR over the sources that report it, as for scalar fields.
    # 4. The best group wins, and its most complete record supplies all the parts, so the
    #    profile never shows a postcode from one record beside a locality from another.
    by_record = defaultdict(list)
    for o in observations:
        by_record[(o["source_id"], o["source_record_id"])].append(o)
    groups = defaultdict(list)  # (state, postcode) -> list of record address dicts
    for (sid, rid), os_ in by_record.items():
        a = {o["field"].split(".", 1)[1]: o for o in os_}
        if not ({"full", "postcode", "locality"} & set(a)):
            continue  # a bare country or state constant is not an address
        key = (a["state"]["value"] if "state" in a else None, a["postcode"]["value"] if "postcode" in a else None)
        if key == (None, None):
            key = ("full", norm_name(a["full"]["value"]) if "full" in a else rid)
        w = sum(_weight(o) for o in a.values()) / len(a)
        groups[key].append({"source_id": sid, "source_record_id": rid, "fields": a, "w": w})
    if not groups:
        return None
    scored = []
    for key, recs in groups.items():
        best_by_source = {}
        for r in recs:
            if r["source_id"] not in best_by_source or r["w"] > best_by_source[r["source_id"]]["w"]:
                best_by_source[r["source_id"]] = r
        p = 1.0
        for r in best_by_source.values():
            p *= 1 - r["w"]
        # The representative record is the most complete, then most trusted, record in the group.
        rep = max(recs, key=lambda r: (len(r["fields"]), r["w"]))
        scored.append({"key": key, "support": round(1 - p, 4), "rep": rep, "sources": sorted(best_by_source),
                       "n_records": len(recs)})
    scored.sort(key=lambda s: s["support"], reverse=True)
    w = scored[0]
    total = sum(s["support"] for s in scored)
    agreement = w["support"] / total if total else 0
    fields = w["rep"]["fields"]
    return {
        "value": {f: fields[f]["value"] for f in ADDRESS_FIELDS if f in fields},
        "confidence": {f: round(w["support"] * agreement * (fields[f].get("field_confidence") or 0), 3)
                       for f in ADDRESS_FIELDS if f in fields},
        "confidence_components": {"source_reliability": next(iter(fields.values())).get("source_reliability"),
                                  "link": next(iter(fields.values())).get("link_confidence"),
                                  "mapping_by_part": {f: fields[f].get("field_confidence") for f in ADDRESS_FIELDS if f in fields},
                                  "support": w["support"], "agreement": round(agreement, 3)},
        "provenance": {"source_id": w["rep"]["source_id"], "source_record_id": w["rep"]["source_record_id"],
                       "fields": {f: _prov(fields[f]) for f in ADDRESS_FIELDS if f in fields},
                       "corroborating_sources": w["sources"], "n_records_at_this_address": w["n_records"]},
        "decision": {"support": w["support"], "agreement": round(agreement, 3),
                     "rule": "whole address from one record; the (state, postcode) group with the most source support wins"},
        **({"alternatives": [{"value": {f: s["rep"]["fields"][f]["value"] for f in ADDRESS_FIELDS if f in s["rep"]["fields"]},
                              "support": s["support"], "sources": s["sources"], "n_records": s["n_records"],
                              "note": "other address on file; may be a second real address (premises vs office), not an error"}
                             for s in scored[1:6]]} if len(scored) > 1 else {}),
    }


def build_profile(key, observations, entity, rels):
    """One entity's observations -> one profile. A field goes into `fields` only if something was
    resolved for it, which is how "could not fill" comes out as absent and not as a blank."""
    by_field = defaultdict(list)
    for o in observations:
        by_field[o["field"]].append(o)
    fields = {}
    for f in SCALAR_FIELDS:
        if by_field.get(f"entity.{f}"):
            r = resolve_scalar(f, by_field[f"entity.{f}"])
            if r:
                fields[f] = r
    legal_key = norm_name(fields["legal_name"]["value"]) if "legal_name" in fields else None
    tn = resolve_trading_names(by_field.get("entity.trading_name", []), legal_key)
    if tn:
        fields["trading_names"] = tn
    addr = resolve_address([o for o in observations if o["field"].startswith("address.")])
    if addr:
        fields["address"] = addr
    return {
        "canonical_entity_key": key,
        "sources": sorted({o["source_id"] for o in observations}),
        "n_source_records": len({(o["source_id"], o["source_record_id"]) for o in observations}),
        "fields": fields,
        "links": [{"source_id": m["source_id"], "source_entity": m["source_entity"], "n_records": m["n_records"]}
                  for m in entity["members"]],
        **({"relationships": rels} if rels else {}),
        "profile_version": PROFILE_VERSION,
        "matcher_version": MATCHER_VERSION,
        "as_of": AS_OF or dt.date.today().isoformat(),
    }


def _signature(p):
    """What a reader would notice changing: each field's winning value and its confidence."""
    sig = {}
    for f, v in p["fields"].items():
        if f == "trading_names":
            sig[f] = (tuple(sorted(norm_name(t["value"]) for t in v)), round(sum(t["confidence"] for t in v), 3))
        elif f == "address":
            sig[f] = (json.dumps(v["value"], sort_keys=True), round(sum(v["confidence"].values()) / len(v["confidence"]), 3))
        else:
            sig[f] = (_value_key(f, v["value"]), v["confidence"])
    return sig


def _same_winner(f, x, y):
    if f == "legal_name":
        return same_name_variant(x, y)
    return x == y


def _compare(p, q):
    """How profile q differs from profile p, in the terms a reader would notice."""
    a, b = _signature(p), _signature(q)
    lost = sorted(set(a) - set(b))
    changed = sorted(f for f in set(a) & set(b) if not _same_winner(f, a[f][0], b[f][0]))
    conf_delta = {f: round(b[f][1] - a[f][1], 3) for f in sorted(set(a) & set(b))
                  if f not in changed and abs(b[f][1] - a[f][1]) >= 0.05}
    return lost, changed, conf_delta


def _uid(rec):
    return f"{rec['source_id']}::{rec['source_entity']}"


def _biggest_component(uids, links):
    """The connected part of an entity, among these source entities, spanning the most sources."""
    # Removing a source can cut an entity into pieces: if A-B and B-C were its links and B is
    # deleted, nothing joins A to C any more. This rebuilds the link graph without the deleted
    # source's members and walks it to find which members are still joined to each other.
    adj = defaultdict(set)
    for l in links:
        a, b = _uid(l["source_a_record"]), _uid(l["source_b_record"])
        if a in uids and b in uids:
            adj[a].add(b)
            adj[b].add(a)
    best, seen = set(), set()
    for u in sorted(uids):
        if u in seen:
            continue
        stack, comp = [u], set()
        while stack:
            x = stack.pop()
            if x not in comp:
                comp.add(x)
                stack.extend(adj[x] - comp)
        seen |= comp
        if len({c.split("::")[0] for c in comp}) > len({c.split("::")[0] for c in best}):
            best = comp
    return best


def _rests_on_alone(profiles, s):
    """Fields whose whole value comes from this one source and that no other source contests.
    If this source is wrong about them, nothing else in the profile can show it."""
    n = 0
    for p in profiles:
        for f, v in p["fields"].items():
            if f in ("trading_names",):
                n += sum(t["provenance"]["sources"] == [s] for t in v)
            elif f == "address":
                n += v["provenance"]["corroborating_sources"] == [s] and "alternatives" not in v
            elif v["provenance"]["sources"] == [s] and "disagreements" not in v:
                n += 1
    return n


def source_deletion(profiles, obs_by_key, entities, rels_by_key):
    """Delete one source at a time: drop its observations and its links, recompute connectivity
    and every field. Report how many profiles change and how badly.

    Nearly every entity spans exactly two sources, so deleting either one mostly unlinks it. That says
    little on its own, so we also rebuild each unlinked profile from the surviving source and report
    what a reader would have seen change, and we measure the same deletion across all linked entities."""
    links = _jsonl(LINK_DIR / "links.jsonl")
    links_by_key = defaultdict(list)
    for l in links:
        links_by_key[l["canonical_entity_key"]].append(l)
    ent = {e["canonical_entity_key"]: e for e in entities}
    out = {}
    # For each source in turn, pretend it was never loaded. Each profile then ends one of three ways:
    #   unchanged       the profile did not use that source, or lost nothing a reader would notice
    #   unlinked        what is left no longer spans two sources, so the entity would not exist
    #   fields_changed  still linked, but a field vanished, a winner changed or a confidence moved
    for s in sorted({sid for e in entities for sid in e["sources"]}):
        res = {"unchanged": 0, "fields_changed": 0, "unlinked": 0, "details": []}
        for p in profiles:
            key = p["canonical_entity_key"]
            if s not in p["sources"]:
                res["unchanged"] += 1
                continue
            uids = {f"{m['source_id']}::{m['source_entity']}" for m in ent[key]["members"] if m["source_id"] != s}
            biggest = _biggest_component(uids, links_by_key[key])
            if len({u.split("::")[0] for u in biggest}) < 2:
                res["unlinked"] += 1
                remaining = sorted({u.split("::")[0] for u in uids})
                q = build_profile(key, [o for o in obs_by_key[key] if o["source_id"] != s], ent[key], rels_by_key.get(key))
                lost, changed, conf_delta = _compare(p, q)
                res["details"].append({"entity": key, "effect": "no longer links two sources; the entity would not exist",
                                       "remaining_sources": remaining,
                                       "if_kept_as_a_single_source_record": {"fields_lost": lost, "winner_changed": changed,
                                                                              "confidence_moved": conf_delta}})
                continue
            keep_sources = {u.split("::")[0] for u in biggest}
            obs = [o for o in obs_by_key[key] if o["source_id"] in keep_sources]
            q = build_profile(key, obs, ent[key], rels_by_key.get(key))
            lost, changed, conf_delta = _compare(p, q)
            if lost or changed or conf_delta:
                res["fields_changed"] += 1
                res["details"].append({"entity": key, "fields_lost": lost, "winner_changed": changed,
                                       "confidence_moved": conf_delta})
            else:
                res["unchanged"] += 1
        res["profiles_affected"] = res["fields_changed"] + res["unlinked"]
        unlinked = [d["if_kept_as_a_single_source_record"] for d in res["details"] if "if_kept_as_a_single_source_record" in d]
        res["of_unlinked_winner_would_change"] = sum(bool(u["winner_changed"]) for u in unlinked)
        res["of_unlinked_field_would_vanish"] = sum(bool(u["fields_lost"]) for u in unlinked)
        res["fields_resting_on_this_source_alone"] = _rests_on_alone(profiles, s)
        touching = [e for e in entities if s in e["sources"]]
        lost_entities = 0
        for e in touching:
            uids = {f"{m['source_id']}::{m['source_entity']}" for m in e["members"] if m["source_id"] != s}
            if len({u.split("::")[0] for u in _biggest_component(uids, links_by_key[e["canonical_entity_key"]])}) < 2:
                lost_entities += 1
        res["all_entities"] = {"touching_this_source": len(touching), "would_no_longer_link": lost_entities,
                               "of_all_entities": len(entities),
                               "links_lost": sum(s in (l["source_a_record"]["source_id"], l["source_b_record"]["source_id"])
                                                 for l in links)}
        out[s] = res
    return out


def deletion_note(deletion, profiles):
    """The short written note Part 4 asks for, with this run's numbers."""
    short = lambda sid: sid.split(":")[1]
    L = ["# What happens when a source is wrong", "",
         f"Generated by `python -m firmable profiles` over {len(profiles)} profiles, as of "
         f"{profiles[0]['as_of'] if profiles else 'n/a'}.", "",
         "**If a source is deleted.** Almost every linked entity spans exactly two sources, so deleting either "
         "one mostly removes the link and the profile with it. The first number below is therefore close to "
         "\"how many profiles used this source\". The columns after it measure the damage to the values.", "",
         f"| Deleted source | Profiles affected (of {len(profiles)}) | No longer linked | Fields changed, still linked | "
         "Of the unlinked: winning value would change | Of the unlinked: a field would vanish | "
         "All entities no longer linked | Links lost |",
         "|---|---|---|---|---|---|---|---|"]
    for s, r in deletion.items():
        a = r["all_entities"]
        L.append(f"| {short(s)} | {r['profiles_affected']} | {r['unlinked']} | {r['fields_changed']} | "
                 f"{r['of_unlinked_winner_would_change']} | {r['of_unlinked_field_would_vanish']} | "
                 f"{a['would_no_longer_link']} of {a['of_all_entities']} | {a['links_lost']} |")
    L += ["", "**If a source is wrong but still present.** Deleting it is the easy case. The harder one is a source "
          "that stays and is wrong. How much a wrong source can do depends on what it claims:", "",
          "- *A wrong value on a contested field* shows up: it loses or wins on support, and the losing value and the "
          "reason are in the profile. Reliability scores are the agent's opinion, not measured, so a wrong source "
          "rated highly can still win.",
          "- *A wrong value nobody else states* is invisible. Each profile field records its sources, and the count "
          "below is how many fields rest on one source alone, with no other source to contradict it.",
          "- *A wrong identifier* is the most damaging, because an exact ABN match links at high confidence. A checksum "
          "cannot catch a valid ABN on the wrong row. The ACT invoices show this is real (see `outputs/audit/config_audit.md`). "
          "A link refusal only triggers when names also disagree, so a wrong ABN beside a matching name would still merge.", "",
          "| Source | Profile fields resting on it alone |", "|---|---|"]
    L += [f"| {short(s)} | {r['fields_resting_on_this_source_alone']} |" for s, r in deletion.items()]
    return "\n".join(L) + "\n"


def run(n=50, allow_unapproved=False, as_of=None):
    """Part 4 end to end: pick n entities, gather their observations, build the profiles, run the
    source-deletion test, and write profiles.jsonl, source_deletion.json, SOURCE_DELETION.md and
    summary.json."""
    global AS_OF
    AS_OF = as_of or dt.date.today().isoformat()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    entities = _jsonl(LINK_DIR / "entities.jsonl")
    picked = select_entities(n)
    keys = {e["canonical_entity_key"] for e in picked}
    rels = _jsonl(LINK_DIR / "relationships.jsonl") if (LINK_DIR / "relationships.jsonl").exists() else []
    rels_by_key = defaultdict(list)
    for r in rels:
        for k in (r.get("to_entity"), r.get("from_entity")):
            if k in keys:
                rels_by_key[k].append({x: r[x] for x in ("kind", "from_name", "from_entity", "to_name", "to_entity",
                                                          "evidence", "confidence")})
    print(f"collecting observations for {len(picked)} entities")
    obs_by_key = collect_observations(keys, allow_unapproved)
    profiles = [build_profile(e["canonical_entity_key"], obs_by_key[e["canonical_entity_key"]], e,
                              rels_by_key.get(e["canonical_entity_key"])) for e in picked]
    with open(PROFILE_DIR / "profiles.jsonl", "w", encoding="utf-8") as f:
        for p in profiles:
            f.write(json.dumps(p, ensure_ascii=False, default=str) + "\n")
    deletion = source_deletion(profiles, obs_by_key, entities, rels_by_key)
    (PROFILE_DIR / "source_deletion.json").write_text(json.dumps(deletion, indent=2, ensure_ascii=False), encoding="utf-8")
    (PROFILE_DIR / "SOURCE_DELETION.md").write_text(deletion_note(deletion, profiles), encoding="utf-8")
    contested = sum(any("disagreements" in v for v in p["fields"].values() if isinstance(v, dict)) for p in profiles)
    summary = {"profiles": len(profiles), "as_of": AS_OF, "matcher_version": MATCHER_VERSION,
               "profiles_with_a_contested_field": contested,
               "contested_by_field": {f: sum("disagreements" in p["fields"].get(f, {}) for p in profiles)
                                      for f in SCALAR_FIELDS if any("disagreements" in p["fields"].get(f, {}) for p in profiles)},
               "field_fill": {f: sum(f in p["fields"] for p in profiles)
                              for f in SCALAR_FIELDS + ["trading_names", "address"]},
               "source_deletion": {s: {k: v for k, v in r.items() if k != "details"} for s, r in deletion.items()}}
    (PROFILE_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return summary
