# Review: System Procurement_Contract Disclosure Report

- **Publisher:** Queensland Health
- **Licence:** cc-by-4
- **Resource:** https://www.data.qld.gov.au/dataset/2b977e4d-c765-4ce5-8088-65210a744add/resource/09335d0c-cfba-43e0-b036-4461c8345a73/download/sp_contract-disclosure-report-2025-26.csv
- **Agent says it is:** A Queensland Health (System Procurement Branch) contract disclosure register for 2025-26, listing current contracts and standing offer arrangements valued over $10,000. It is a comma-delimited CSV with a single header row and 25 columns, including supplier name, supplier address, supplier ABN, contract value, award date, commence date and finish date.
- **Record grain:** one record = one contract or standing offer arrangement disclosure line (one supplier per contract line). Contract reference number is not unique, because the HSQ97217-n and HSQ109250 families share a parent number, so identity needs a composite key or a row hash.
- **Contains business entities:** True (The Supplier name column holds registered-style company names (Everlight Radiology Pty Ltd, Becton Dickinson Pty Ltd, Queensland X-Ray Pty Ltd). The Supplier ABN column holds spaced 11-digit ABNs (e.g. '38 120 630 784'), and Supplier address holds full street addresses with state and postcode. The agency (Queensland Health) is also a business entity, but it is the buyer, not the supplier.)
- **Reader:** `{"format": "csv", "encoding": "cp1252", "delimiter": ",", "header_row": 0}`
- **Record id:** `{"strategy": "row_hash", "rationale": "No stable key. Contract reference number repeats across suppliers (109843 x17, HSQ84570 x8), has typos and placeholders ('TBC', 'HSQ109250 -4'), and sometimes holds several references in one cell. Even reference + supplier + dates can collide: Becton Dickinson HSQ102882-1 appears twice with different award dates. A hash of the full row is deterministic and traces back to one physical record."}`
- **Source reliability (agent's proposal):** 0.8 (Official agency disclosure register under CC-BY-4.0. Supplier names and ABNs are mostly well-formed, but entry is manual: typos (e.g. 'Austalia'), a few percent of ABNs fail checksum, placeholder suppliers, and several rows pack multiple suppliers into one cell.)
- **Validation on 101 unseen records:** PASSED
- **Agent cost:** 7 calls, 82931 in / 33131 out tokens, $0.4588, 210.6s, 3 revise round(s)

**Agent's concerns:**

- Some quoted cells contain embedded newlines (e.g. the SOA708 record's Contract description splits across physical lines). The reader must be a proper quote-aware CSV parser, not line-based, or those rows will be split in two.
- Encoding is uncertain. The sample shows U+FFFD replacement characters ('Estimated �contract value�'), so the file is probably Windows-1252 or already damaged upstream. Decoding as cp1252 may not recover the characters, because the damage may be in the source bytes. Check the raw bytes before choosing; utf-8 with replacement is the fallback, and only free-text description fields are affected.
- Many rows have Supplier name = 'Refer to Queensland Government Arrangements Directory' with blank address and ABN. This is a placeholder, not a business name, and must be nulled or dropped, never emitted as a legal_name.
- Supplier name often has a bracketed trading name or brand appended, e.g. 'Premier Medical Imaging Pty Ltd (Qscan)' and 'I-Telerad Pty Ltd (I-Med)'. The legal name and trading name are mixed in one cell and need a careful split.
- Supplier ABN is formatted with spaces and is blank for many rows. It must be checksum-validated, not just length-checked.
- Dates are d/m/yyyy, non-zero-padded and day-first. Contract value is a string with '$' and thousands commas. Several rows repeat the same estimated value across suppliers in a shared arrangement, so it is not a per-supplier amount.
- Supplier address is a single free-text string with state and postcode embedded, so it would need parsing to populate address.state and address.postcode and is not safely split with the available transforms.
- The Agency columns describe the buyer (Queensland Health), the same on every row. They should not be mapped as the supplier entity.
- Contract reference number has typos and inconsistencies (e.g. 'HSQ109250 -4' with a stray space), so it is not a clean key.
- The sample shows only the first rows, so I cannot confirm whether trailing blank rows, totals or footnotes exist further down.

## Mappings

| canonical | source | transforms | conf | holdout coverage | error rate | rationale |
|---|---|---|---|---|---|---|
| entity.legal_name | `Supplier name` | strip, collapse_ws, null_if, regex_extract | 0.7 | 90/101 | 0.0 | About 80% of values carry Pty Ltd/Limited/Inc style suffixes, so these are mostly registered names. The placeholder is nulled, and a trailing '(Trading As ...)' is cut off so only the registered part is kept. Some values have no company suffix (e.g. 'Kerr Australia', 'Switch Transformation Consultancy'), so those may be trading names or sole traders and the confidence is held down. |
| entity.abn | `Supplier ABN` | strip, digits_only, length_in, abn | 0.95 | 80/101 | 0.035 | Spaced 11-digit ABNs. Digits are extracted, anything that is not exactly 11 digits (multi-ABN cells, 12-digit typos) gives no value, and the real ABN checksum is then enforced. Cells that fail the checksum (about 3.5% in the holdout) are dropped, not emitted. |
| address.full | `Supplier address` | strip, collapse_ws, null_if | 0.85 | 81/101 | 0.0 | Free-text street address of the supplier. The placeholder is nulled. Embedded newlines are collapsed. A few cells hold two addresses separated by ';', which are kept as one string and flagged in the notes. |
| address.state | `Supplier address` | strip, collapse_ws, null_if, regex_extract, state | 0.85 | 73/101 | 0.0 | Takes the state token immediately before a trailing 4-digit postcode. It only matches a recognised AU state name or abbreviation (case-insensitive), then normalises it. Multi-address cells (containing ';'), placeholders, blanks, addresses with no state token and non-AU addresses give no value instead of a guess. |
| address.postcode | `Supplier address` | strip, collapse_ws, null_if, regex_extract, postcode | 0.85 | 73/101 | 0.0 | Takes the 4-digit postcode only when it directly follows an AU state token. This keeps NZ and overseas codes out of an AU field, and multi-address cells are excluded so one address's postcode is not attributed to the other. |

## Unmapped source fields (22)

- `Agency (Dept or Stat Body)`: Buyer (Queensland Health), identical on every row. Not the supplier entity. Captured as a relationship instead.
- `Agency address`: Buyer address, identical on every row. Must not be attached to the supplier.
- `Description`: Contract free text with embedded newlines and truncation. No canonical field. Not a business name.
- `Contract description/name`: Contract title. No canonical field.
- `Award contract date`: Date the contract was awarded, not when the source stated the claim. Some values are after the dataset's last-modified date (e.g. 1/07/2026), so it is not safe as observed_at.
- `Contract value`: Mixed free text ('$10,000 or more', '$90,000,000 (8 years)', bare numbers). Repeated across suppliers in shared arrangements. No canonical field in this ontology.
- `Variation to contract (Yes/No)`: Contract attribute, no canonical field.
- `Specific confidentiality provision used`: Contract attribute, no canonical field.
- `Procurement method`: Contract attribute, no canonical field.
- `Reason for use of limited tendering`: Contract free text, no canonical field.
- `Form of contract`: Contract attribute, no canonical field.
- `Number of offers sought`: Free-text contract attribute, no canonical field.
- `Evaluation criteria and weightings`: Contract attribute, no canonical field.
- `Deliverables`: Contract attribute, no canonical field.
- `Contract milestones`: Contract attribute, no canonical field.
- `Contract performance management`: Contract attribute, no canonical field.
- `Contract reference number`: Not a business identifier. Not unique, with typos, placeholders ('TBC') and multi-value cells, so it is not used as record_id either.
- `Contract category group`: Procurement category, not ANZSIC. Not mapped to industry_code.
- `Parent contract number/SOA`: Parent of the contract, not a parent entity.
- `Variation comments`: Free text about the contract variation.
- `Commence date`: Contract start. It does not say when the supplier's name, ABN or address became true, so it is not used as valid_from. It also has bad values (e.g. 31/12/2029 equal to the finish date) and multi-value cells.
- `Finish date`: Contract end, not the end of a claim about the entity. It contains annotations such as '01/04/2029 (initial term)' and multi-value cells, so it is not used as valid_to.

## Canonical fields left empty (13)

- `entity.trading_name`: The only signal is an explicit '(Trading As X)' inside Supplier name, which appears in about 1 of 101 holdout rows. The extraction needed required:true, which errors on every other row (99% error rate). A non-required variant risks emitting the legal name as a trading name, so it is left unmapped.
- `entity.acn`: No ACN column. Taking the last 9 digits of the ABN would assert an ACN for trusts, sole traders and partnerships, where it is not one. Left for the resolver.
- `entity.nzbn`: Australian source, not present.
- `entity.entity_type`: No type column. The names include trustee-style cells ('PTY LTD as Trustee for The ... Family Trust') and names with no suffix, and a wrong type does downstream harm. Left as the ontology default 'unknown'.
- `entity.status`: Contract being current says nothing reliable about entity registration status. Default 'unknown'.
- `entity.website`: Not present.
- `entity.industry_code`: No ANZSIC or other industry code in the source.
- `entity.date_registered`: Not present.
- `address.locality`: Locality is not reliably separable from the free-text address (variable street/suburb formats, PO Boxes, 'c/-' prefixes).
- `address.country`: No country column, and suppliers could in principle be overseas. The ontology default AU applies and is not asserted here.
- `observation.observed_at`: No per-record statement date. The engine falls back to the resource's last-modified date.
- `observation.valid_from`: Contract dates do not describe when the entity claims became true.
- `observation.valid_to`: Contract dates do not describe when the entity claims stopped being true.

## Relationship hints

- `Agency (Dept or Stat Body)` (other): Buyer/customer (Queensland Health) in a contract with the supplier on this row. A procurement relationship, not parent or agent. Do not treat as a parent of the supplier.

## What the agent changed while revising

- Round from v1 (passed before: False): 
  - Flattened transform arguments in every transform that had them (null_if, regex_extract, and the new length_in). They now sit at the top level of the transform object, e.g. {"op": "regex_extract", "pattern": ..., "group": 1}, instead of under an "args" key.: The engine raised KeyError: 'pattern' on all 101 rows of legal_name, trading_name, address.state and address.postcode. The vocabulary shape is {"op": name, ...args}, so I had wrapped the arguments wrongly. The null_if on address.full did not error, but its args were nested too and may have been silently ignored, so I flattened it as well.
  - Changed the ABN transforms to strip, digits_only, length_in [11], abn.: The ABN error rate was 0.07, above the 0.05 limit. Some of the failures were not checksum failures at all. They were cells that cannot be a single ABN, such as the semicolon-joined multi-ABN cell (33 digits) and 12-digit values like '520 583 909 917'. These now route to 'no value' instead of a parse error, and they are still never emitted. Genuine 11-digit checksum failures (e.g. 86 087 097 717) are still rejected by the abn transform. I did not loosen the checksum, because emitting an invalid ABN is worse than dropping it. The remaining error rate is real source data-entry error.
  - Updated the ABN rationale and notes to say that residual checksum failures are genuine source errors.: Reviewers should know that any remaining ABN errors come from the data and are not a mapping bug.
  - Checked the preview against meaning. No mappings were added or removed, and confidences are unchanged.: The address.full values are supplier street addresses, and the ABNs shown are plausible. The preview rows for legal_name, trading_name, state and postcode all errored, so those values still need checking on the next run. Names like 'Kerr Australia' have no company suffix, so I kept legal_name at 0.7 and flagged it.
- Round from v2 (passed before: False): 
  - Removed the entity.trading_name mapping and moved it to unmapped_canonical_fields.: It had a 99% error rate because regex_extract with required:true raises an error on every row without '(Trading As ...)'. Only 1 of 101 rows produced a value. Setting required:false is not a safe fix, because I can't confirm what the engine emits on a non-match. If it passed the whole cell through, the legal name would be emitted as a trading name, which is worse than no mapping. Prefer unmapping over a fragile transform.
  - Added a null_if for 'Refer to Queensland Government Arrangements Directory' to the start of the address.state and address.postcode chains.: The placeholder rows were a direct source of errors (error rate 0.141, above the 0.05 limit). They are now treated as missing, as address.full already does.
  - Set required:false on the regex_extract in address.state and address.postcode.: Addresses with no state token or no trailing postcode, and multi-address cells, are expected and should give no value rather than a parse error. Examples are '805/220 Collins Street, Melbourne', '77-79 Ricketts Rd Mt Waverley', 'Bowen Hills 4006' and the ';'-separated cells. The regex stays anchored to 'STATE postcode' at the end, so it still never guesses. If a non-match fell through unchanged, the following state/postcode transform would reject it, so no wrong value could be emitted.
  - Updated the notes.: Recorded that trading_name is dropped, that '(Trading As X)' text is now stripped from legal_name and lost, and that the reviewer should confirm required:false gives no value on a non-match.
- Round from v3 (passed before: True): no changes, signed off

## Preview: unseen records, raw → canonical

**Record 1**

- entity.legal_name: `Eppendorf South Pacific Pty Ltd` → `['Eppendorf South Pacific Pty Ltd']`
- entity.abn: `` → `[]`
- address.full: `Level 1, 97 Waterloo Road, Macquarie Park, NSW 2113` → `['Level 1, 97 Waterloo Road, Macquarie Park, NSW 2113']`
- address.state: `Level 1, 97 Waterloo Road, Macquarie Park, NSW 2113` → `['NSW']`
- address.postcode: `Level 1, 97 Waterloo Road, Macquarie Park, NSW 2113` → `['2113']`

**Record 2**

- entity.legal_name: `Refer to Queensland Government Arrangements Directory` → `[]`
- entity.abn: `` → `[]`
- address.full: `` → `[]`
- address.state: `` → `[]`
- address.postcode: `` → `[]`

**Record 3**

- entity.legal_name: `Amgen Australia Pty Ltd` → `['Amgen Australia Pty Ltd']`
- entity.abn: `31 051 057 428` → `['31051057428']`
- address.full: `Level 11, 10 Carrington Street, Sydney NSW 2000` → `['Level 11, 10 Carrington Street, Sydney NSW 2000']`
- address.state: `Level 11, 10 Carrington Street, Sydney NSW 2000` → `['NSW']`
- address.postcode: `Level 11, 10 Carrington Street, Sydney NSW 2000` → `['2000']`

**Record 4**

- entity.legal_name: `Baxter Healthcare Pty Ltd` → `['Baxter Healthcare Pty Ltd']`
- entity.abn: `43 000 392 781` → `['43000392781']`
- address.full: `Unit 2/1115 Stanley Street, East Coorparoo QLD 4151` → `['Unit 2/1115 Stanley Street, East Coorparoo QLD 4151']`
- address.state: `Unit 2/1115 Stanley Street, East Coorparoo QLD 4151` → `['QLD']`
- address.postcode: `Unit 2/1115 Stanley Street, East Coorparoo QLD 4151` → `['4151']`

**Record 5**

- entity.legal_name: `Kerr Australia` → `['Kerr Australia']`
- entity.abn: `21 003 929 100` → `['21003929100']`
- address.full: `Unit 6/12 Mars rad Lane Cove West, NSW 2066` → `['Unit 6/12 Mars rad Lane Cove West, NSW 2066']`
- address.state: `Unit 6/12 Mars rad Lane Cove West, NSW 2066` → `['NSW']`
- address.postcode: `Unit 6/12 Mars rad Lane Cove West, NSW 2066` → `['2066']`

## Agent notes

- Revision v3: entity.trading_name is unmapped (99% error rate with required:true, and a non-required version could pass the legal name through). Because '(Trading As X)' is still stripped from legal_name, a trading name that appears in that text is lost in this config. A later version could emit it once the engine has a safe way to extract it.
- Revision v3: address.state and address.postcode now null the placeholder first and use required:false on the regex. Rows without a state-plus-postcode ending, and multi-address cells, should give no value rather than an error. Please check on the next report that a non-match yields no value, not a pass-through of the whole cell. If it passes through, the state/postcode transform will reject it and the rows will count as errors again.
- legal_name had 0 errors on the holdout (90 of 101 records have a value, and the rest are the placeholder). ABN had a 3.5% error rate, all genuine checksum failures in the source (e.g. 86 087 097 717, 96 126 888 259, 34 161 464 441). They are dropped on purpose.
- Parse with a quote-aware CSV reader. Quoted cells contain embedded newlines, including in Supplier address, Contract reference number and Variation comments.
- Check the raw bytes for encoding (U+FFFD seen in free-text columns). Only free-text columns are affected. The mapped columns are ASCII in the samples.
- About 10% of rows have the placeholder 'Refer to Queensland Government Arrangements Directory' as the supplier. They get no legal_name and usually no ABN or address, so they will carry few or no entity claims.
- Multi-supplier rows exist (e.g. Elite Nursing Professionals row with two ABNs, two addresses and two contract refs, with one name). The ABN is skipped, and state and postcode are skipped. address.full is kept as one string containing both addresses. Please review these rows. A later version may need row-level splitting.
- Some legal_name values have no company suffix (e.g. 'Kerr Australia'). They may be trading names, which is why legal_name confidence is 0.7.
- Some addresses lack a state or postcode (e.g. '805/220 Collins Street, Melbourne', '77-79 Ricketts Rd Mt Waverley', 'Bowen Hills 4006'). State and postcode are deliberately not inferred from the suburb or a bare postcode, so only address.full is emitted for these.
- Names such as 'X PTY LTD as Trustee for The Y Family Trust' come through as legal_name with the trust part included. A trustee relationship would be better modelled once the engine can extract it.
- legal_name is source-as-stated, including typos (e.g. 'ICU Medical Austalia Pty Ltd'). Do not correct at ingest.
- The same supplier appears on many rows (e.g. Slade Health x12). This yields many observations per entity, which is expected and left to reconciliation.
- Dates, contract value and contract reference are deliberately not mapped, so this config emits entity and address claims only. If valid_from/valid_to are wanted for 'supplier on contract' claims, that should be a separate relationship or contract observation type, not an attribute validity window.
- Licence: cc-by-4 from CKAN metadata.

---
Approve: `python -m firmable review system_procurement_contract-disclosure-report --approve --reviewer <name>`  
Reject: `python -m firmable review system_procurement_contract-disclosure-report --reject --note "why"`  
Send back with feedback: `python -m firmable review system_procurement_contract-disclosure-report --feedback "what to change"`
