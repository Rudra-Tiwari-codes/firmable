# Review: QBCC Licensed Contractors Register

- **Publisher:** Queensland Building and Construction Commission
- **Licence:** CC-BY-4.0
- **Resource:** https://www.data.qld.gov.au/dataset/980b6499-c0b4-491b-ba9c-1c7506368a50/resource/25608781-b28c-44f8-8545-0ab18d84082f/download/20260923_qbcc_active_licencees_opendatain-2.csv
- **Agent says it is:** A CSV extract of the Queensland Building and Construction Commission (QBCC) register of active licensees, covering builders, trade contractors, nominee supervisors and similar licence holders. The file is denormalised: each row is one licence-class entry for a licensee, not one licensee.
- **Record grain:** one record = one licence number x licence class (Licence Class Type) combination. A single licensee appears on many rows. Example: licence 1140280 spans 7 rows, one per trade class. The 'id' column is a sequential row number, not a licensee or licence key.
- **Contains business entities:** True (The Company rows carry registered names (e.g. 'RAINVALE PTY. LIMITED') with ACN and ABN (010 637 877 / 78 010 637 877), a business address, and Licence Type DESC 'Company'. The file also holds many 'Individual' rows (e.g. GREGORY LAWRENCE TOWNLEY, DARREN ROSS LOVERIDGE), which are natural persons. Some of these have an ABN (sole traders), and many have no ABN. Nominee Supervisor rows are people nominated to supervise, not necessarily the licensed business.)
- **Reader:** `{"format": "csv", "encoding": "utf-8", "delimiter": ",", "header_row": 0}`
- **Record id:** `{"strategy": "fields", "fields": ["Licence Number", "Licence Grade", "Licence Class Type"], "rationale": "The grain is licence number x licence class. Licence Number alone would collide, since one licence spans many class rows. Licence Grade is added because a class name such as 'Builder - Open' appears under both Builder Licence and Site Supervisor. The 'id' column is a sequential row number that is unlikely to be stable across publications, so it is not used. The holdout of 500 rows showed 0 duplicate keys, but uniqueness over the full file is unverified, and a reviewer should check it."}`
- **Source reliability (agent's proposal):** 0.85 (Statutory regulator register, authoritative for licensee name, ABN/ACN and licence status. Lowered slightly because the extract is a denormalised snapshot, and the generation date and download header disagree.)
- **Validation on 500 unseen records:** PASSED
- **Agent cost:** 5 calls, 45786 in / 17651 out tokens, $0.2477, 112.7s, 2 revise round(s)

**Agent's concerns:**

- Row grain is licence x class, so naive row-per-entity extraction will emit many duplicate observations per licensee. Dedupe or group on Licence Number downstream. A source_record_id built from Licence Number alone would collide, so it should include the class, or use the 'id' column with the caveat below.
- The 'id' column is a sequential row number and probably not stable across publications. Licence Number plus Licence Class Type is a better deterministic key, but it should be checked for uniqueness on the full file.
- Many licensees are individuals (natural persons). Their names are personal data, not business names. Individuals with an ABN may be sole traders, but the entity type should not be guessed from Licence Type alone.
- ABN and ACN are formatted with spaces ('81 804 232 956', '010 637 877'), so they need digit normalisation before checksum validation. ABN is blank for many individuals, and ACN is blank for all non-companies.
- The address is a single free-text string with the state and postcode at the end. It has messy values such as 'Fy 4  5 Ramly Dr' and '24 -26 Little Ave', with irregular whitespace. Splitting it into locality, state and postcode needs care.
- The file has no per-record date column. Observed_at will fall back to the resource's last-modified date. The download header says 30 Oct 2026, while the resource says it is current as at 23/09/2026 and the metadata says 2026-09-23. These dates disagree and should be reconciled by a reviewer.
- The dataset note says suspended licences are included but cancelled ones are not, and there is no status column. Do not map status to 'active', because suspended licensees are mixed in and indistinguishable.
- Licence is stated as CC-BY-4.0 in the metadata.
- The file is about 37 MB, so it is large but not truncated. Only the first 14 lines were seen, so the encoding is assumed to be UTF-8 and was not verified beyond that sample.

## Mappings

| canonical | source | transforms | conf | holdout coverage | error rate | rationale |
|---|---|---|---|---|---|---|
| entity.legal_name | `Licensee Name` | strip, collapse_ws | 0.7 | 500/500 | 0.0 | Licensee name on a regulator register. For company rows (about 36%, with a Pty Ltd/Ltd suffix and an ACN) it is the registered name. For Individual rows it is the person's own name, likely matching their ABN entity name but not a registered business name. Confidence is lowered because the column mixes both. |
| entity.entity_type | `Licensee Name` | strip, collapse_ws, entity_type_from_name | 0.8 | 500/500 | 0.0 | Company is set only from unambiguous legal suffixes, otherwise unknown. 'Individual' is deliberately not mapped to sole_trader, because the licence type does not establish that. |
| entity.acn | `ACN` | strip, null_if, acn | 0.95 | 174/500 | 0.006 | Space-formatted ('636 110 962'). The acn op strips the spaces and checks the checksum (about 99.4% valid in the holdout). Blank for non-companies. |
| entity.abn | `ABN` | strip, null_if, abn | 0.95 | 312/500 | 0.003 | Space-formatted ('75 158 361 912'). The abn op strips the spaces and checks the checksum (about 99.7% valid in the holdout). Blank for many individuals. |
| address.full | `Licensee Business Address` | strip, collapse_ws | 0.9 | 500/500 | 0.0 | Single free-text address string with irregular whitespace. Whitespace is normalised and nothing else is changed. |
| address.state | `Licensee Business Address` | strip, collapse_ws, regex_extract, upper, state | 0.9 | 500/500 | 0.0 | The state, as an abbreviation or full name, sits immediately before the 4-digit postcode at the end of the string. Addresses that do not fit the pattern are dropped rather than guessed. |
| address.postcode | `Licensee Business Address` | strip, collapse_ws, regex_extract, postcode | 0.9 | 500/500 | 0.0 | The trailing 4 digits of the address are the postcode (500/500 in the holdout). |
| address.country | const `AU` | - | 0.85 | 500/500 | 0.0 | Queensland regulator register. Every sampled address ends in an AU state and postcode. Not verified across the full file. |

## Unmapped source fields (8)

- `id`: Sequential row number, not a licensee or licence key, and probably unstable across publications. Not used in the record id.
- `Licence Number`: Used in the record id. There is no canonical field for a licence identifier. It is not an ABN or ACN.
- `Licence Type DESC`: Describes the licence holder category (Company/Individual). It is not used for entity_type because Individual does not establish sole_trader. It matches the ACN and company-suffix rates and is useful as evidence only.
- `Licence Type CODE`: Duplicates Licence Type DESC (C/I).
- `Financial Category DESC`: Financial reporting category for the licence. No canonical field.
- `Financial Category CODE`: Code for Financial Category. No canonical field.
- `Licence Grade`: Licence kind (Builder, Trade Contractor, Nominee Supervisor, etc.). No canonical field. It does not indicate entity status.
- `Licence Class Type`: QBCC trade or licence class, not ANZSIC. Mapping it to industry_code would be wrong. Used only in the record id.

## Canonical fields left empty (10)

- `entity.trading_name`: No trading or business name column.
- `entity.nzbn`: AU-only source.
- `entity.status`: No status column. The dataset includes Suspended licences but excludes Cancelled ones, and they cannot be told apart, so 'active' cannot be asserted.
- `entity.website`: Not present.
- `entity.industry_code`: No ANZSIC value. Licence Class Type is a different taxonomy.
- `entity.date_registered`: No registration or licence start date.
- `address.locality`: Locality is embedded in free text after the street with no delimiter ('45 Chetwynd St Loganholme QLD 4129'), so it cannot be split reliably with the available transforms.
- `observation.observed_at`: No per-record date column. The engine falls back to the resource last-modified date.
- `observation.valid_from`: No licence start date column.
- `observation.valid_to`: No licence expiry column.

## What the agent changed while revising

- Round from v1 (passed before: False): 
  - entity.legal_name: removed the regex_extract (required) company-suffix filter. The mapping is now strip + collapse_ws only, with confidence lowered from 0.85 to 0.7.: The filter turned every Individual row (65% of the holdout) into a parse error, so the mapping failed at 0.65 against the 0.05 limit. The filter was also a fragile way to express a judgement. Licensee Name is the licensee's name on a statutory register: for companies it is the registered name (Pty Ltd), and for individuals it is the person's own name, which is also their name on the ABN record. The column is mixed, so confidence is lower. entity_type is derived from the name suffix and stays 'unknown' for individuals. The privacy and individual-name question is flagged for the reviewer.
  - address.state: extended the regex to also accept full state names (Queensland, New South Wales, etc.) before the 4-digit postcode. The upper and state ops then normalise them.: 10 of 500 holdout addresses (2%) ended in 'Queensland 4818' rather than 'QLD 4818' and were dropped. This is under the 0.05 threshold but a systematic, cheap miss. The postcode mapping already handled these rows.
  - Removed the Licensee Name entry from unmapped_source_fields and rewrote the notes about individuals.: The column is now mapped. The notes previously said individual names were not emitted as legal_name, which would no longer be true.
- Round from v2 (passed before: True): no changes, signed off

## Preview: unseen records, raw → canonical

**Record 1**

- entity.legal_name: `NAGAUR HOLDINGS PTY LTD` → `['NAGAUR HOLDINGS PTY LTD']`
- entity.entity_type: `NAGAUR HOLDINGS PTY LTD` → `['company']`
- entity.acn: `003 188 074` → `['003188074']`
- entity.abn: `38 003 188 074` → `['38003188074']`
- address.full: `17 SHEATHER ST BALLINA NSW 2478` → `['17 SHEATHER ST BALLINA NSW 2478']`
- address.state: `17 SHEATHER ST BALLINA NSW 2478` → `['NSW']`
- address.postcode: `17 SHEATHER ST BALLINA NSW 2478` → `['2478']`
- address.country: `AU` → `['AU']`

**Record 2**

- entity.legal_name: `MALCOLM LENARD PALMER` → `['MALCOLM LENARD PALMER']`
- entity.entity_type: `MALCOLM LENARD PALMER` → `['unknown']`
- entity.acn: `` → `[]`
- entity.abn: `` → `[]`
- address.full: `8/11 DOMINION ROAD ASHMORE QLD 4214` → `['8/11 DOMINION ROAD ASHMORE QLD 4214']`
- address.state: `8/11 DOMINION ROAD ASHMORE QLD 4214` → `['QLD']`
- address.postcode: `8/11 DOMINION ROAD ASHMORE QLD 4214` → `['4214']`
- address.country: `AU` → `['AU']`

**Record 3**

- entity.legal_name: `BALLARD BUILDING PTY LTD` → `['BALLARD BUILDING PTY LTD']`
- entity.entity_type: `BALLARD BUILDING PTY LTD` → `['company']`
- entity.acn: `132 898 143` → `['132898143']`
- entity.abn: `97 132 898 143` → `['97132898143']`
- address.full: `11 TOOHEY STREET PACIFIC PINES QLD 4211` → `['11 TOOHEY STREET PACIFIC PINES QLD 4211']`
- address.state: `11 TOOHEY STREET PACIFIC PINES QLD 4211` → `['QLD']`
- address.postcode: `11 TOOHEY STREET PACIFIC PINES QLD 4211` → `['4211']`
- address.country: `AU` → `['AU']`

**Record 4**

- entity.legal_name: `TIMOTHY STEPHEN COOK` → `['TIMOTHY STEPHEN COOK']`
- entity.entity_type: `TIMOTHY STEPHEN COOK` → `['unknown']`
- entity.acn: `` → `[]`
- entity.abn: `` → `[]`
- address.full: `37 Cobbs Road Woombye QLD 4559` → `['37 Cobbs Road Woombye QLD 4559']`
- address.state: `37 Cobbs Road Woombye QLD 4559` → `['QLD']`
- address.postcode: `37 Cobbs Road Woombye QLD 4559` → `['4559']`
- address.country: `AU` → `['AU']`

**Record 5**

- entity.legal_name: `PHILLIP JAMES PRENDERGAST` → `['PHILLIP JAMES PRENDERGAST']`
- entity.entity_type: `PHILLIP JAMES PRENDERGAST` → `['unknown']`
- entity.acn: `` → `[]`
- entity.abn: `48 450 640 420` → `['48450640420']`
- address.full: `204 McKenzie Rd Oakey QLD 4401` → `['204 McKenzie Rd Oakey QLD 4401']`
- address.state: `204 McKenzie Rd Oakey QLD 4401` → `['QLD']`
- address.postcode: `204 McKenzie Rd Oakey QLD 4401` → `['4401']`
- address.country: `AU` → `['AU']`

## Agent notes

- Grain is one row per licence x class, so a licensee appears on many rows (e.g. 19 rows for Wormald Australia Pty Ltd). Identical entity-level observations (name, ABN, ACN, address) will repeat per row. Dedupe or group on Licence Number downstream.
- About 64% of rows are Individuals. legal_name is now emitted for them as the person's name (confidence 0.7), and entity_type stays 'unknown' for them. Their names and business addresses are likely personal data, and the business address may be a home address. Reviewer decision: whether to keep individual names and addresses, or restrict legal_name to company-suffix names (a regex filter was tried and rejected because it produces a parse error on every individual row).
- Individual rows without an ABN have no identifier beyond the licence number, so linking them to entities will be weak. Downstream linking should not rely on name alone.
- Licence is CC-BY-4.0 per the dataset metadata, so no licence flag is needed.
- Date discrepancy for a reviewer to reconcile: metadata_modified is 2026-09-23, the resource says current as at 23/09/2026, and the download header says 30 Oct 2026. observed_at will fall back to the resource last-modified date.
- Suspended licensees are included and cannot be distinguished, so status is deliberately unmapped.
- The address.state regex uses the inline (?i) flag and assumes Python-style regex semantics in the engine. If the engine does not support it, state values will be dropped.
- The state mapping accepts abbreviations and full names. Addresses ending in neither (about 0 in the holdout after this fix) are dropped.
- Only the first 5000 records were profiled, and 500 held-out records were checked. Encoding is assumed to be UTF-8 and was not verified beyond the sample. The file is about 37 MB.
- Name casing is inconsistent (upper-case, mixed-case) and is preserved as-is. A few ABN/ACN values fail the checksum (<1%) and are dropped rather than emitted.

---
Approve: `python -m firmable review qbcc-licensed-contractors-register --approve --reviewer <name>`  
Reject: `python -m firmable review qbcc-licensed-contractors-register --reject --note "why"`  
Send back with feedback: `python -m firmable review qbcc-licensed-contractors-register --feedback "what to change"`
