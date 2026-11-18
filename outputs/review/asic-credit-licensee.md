# Review: ASIC – Credit Licensee Dataset

- **Publisher:** Australian Securities and Investments Commission (ASIC)
- **Licence:** Creative Commons Attribution 3.0 Australia
- **Resource:** https://data.gov.au/data/dataset/fa0b0d71-b8b8-4af8-bc59-0b000ce0d5e4/resource/35953a01-a9a8-4609-8566-c9fa7de465d3/download/credit_lic_202610.csv
- **Agent says it is:** ASIC Credit Licensee Register snapshot: a comma-delimited CSV listing Australian credit licensees, with licence number, name, start/end dates, status, ABN/ACN, AFSL number, locality/state/postcode, lat/lng, EDR scheme membership, business names and a long free-text licence authorisation description.
- **Record grain:** one record = one credit licence (one licensee per row, keyed by CRED_LIC_NUM). Licence number appears to be unique per row in the sampled rows, but uniqueness is not confirmed across the whole file.
- **Contains business entities:** True (Header row is on line 1: REGISTER_NAME,CRED_LIC_NUM,CRED_LIC_NAME,CRED_LIC_START_DT,CRED_LIC_END_DT,CRED_LIC_STATUS,CRED_LIC_ABN_ACN,CRED_LIC_AFSL_NUM,CRED_LIC_STATUS_HISTORY,CRED_LIC_LOCALITY,CRED_LIC_STATE,CRED_LIC_PCODE,CRED_LIC_LAT,CRED_LIC_LNG,CRED_LIC_EDRS,CRED_LIC_BN,CRED_LIC_AUTHORISATIONS. Sample rows show registered company names (RELI CAPITAL LTD, GLOBAL MUTUAL FUNDS PTY LTD, PLANNING PARTNERS PTY. LTD.), 11-digit ABNs, dd/mm/yyyy dates, and status code APPR. CRED_LIC_BN holds a business name (e.g. PLANNING PARTNERS) when present. All values are quoted. The sniffed content is delimited text and matches the declared CSV format.)
- **Reader:** `{"format": "csv", "encoding": "utf-8", "delimiter": ",", "header_row": 0}`
- **Record id:** `{"strategy": "fields", "fields": ["CRED_LIC_NUM"], "rationale": "CRED_LIC_NUM is the ASIC credit licence number. It is non-empty and unique across all 4313 profiled rows (4313 distinct), and one row is one licence, so it traces back to the exact raw record."}`
- **Source reliability (agent's proposal):** 0.9 (ASIC is the statutory regulator and publisher of the credit licence register, so licence and name claims are authoritative. The ABN/ACN column is messy (placeholder '0', dropped leading zeros, about 1% failing checksum), and the address and geocodes are not formally validated.)
- **Validation on 500 unseen records:** PASSED
- **Agent cost:** 4 calls, 46044 in / 13835 out tokens, $0.2085, 92.0s, 1 revise round(s)

**Agent's concerns:**

- CRED_LIC_AUTHORISATIONS is a very long free-text field containing '~' separators (and possibly embedded newlines or commas). It must be read with proper CSV quote handling. The sample was truncated mid-field, so I could not confirm that every row is well-formed.
- CRED_LIC_ABN_ACN is a mixed column that could hold either an 11-digit ABN or a 9-digit ACN. In the samples every value is 11 digits, but the whole file has not been checked. Leading zeros may also have been dropped. Use length routing and checksum validation downstream.
- CRED_LIC_EDRS and possibly CRED_LIC_BN may contain multiple '~'-separated values (e.g. 'AFCA~FOS'), so they may need splitting.
- CRED_LIC_STATUS uses codes such as APPR. Only that one value is seen in the sample, so the full code set is unknown and any status mapping would be a guess until the distinct values are profiled.
- CRED_LIC_STATUS_HISTORY is empty in all sampled rows, so its format is unknown.
- CRED_LIC_END_DT is empty in the sampled current licences. Rows with end dates (ceased or cancelled licences) may exist. The resource is named 'Current', but this has not been verified.
- The snapshot date is 29/10/2026 per the resource description and the Last-Modified header. There is no per-row observed_at column, so the resource's last-modified date will serve as the observation date.
- CRED_LIC_AFSL_NUM equals the licence number in the samples, which suggests it is an AFSL number, not an ACN or ABN. It is not a business identifier we can map.
- The licence is CC BY 3.0 AU. The description says the data is a point-in-time snapshot of the register, updated weekly.

## Mappings

| canonical | source | transforms | conf | holdout coverage | error rate | rationale |
|---|---|---|---|---|---|---|
| entity.legal_name | `CRED_LIC_NAME` | strip, collapse_ws | 0.85 | 500/500 | 0.0 | Registered licensee name. About 90% carry company suffixes (PTY LTD, LIMITED). The rest look like individuals in 'SURNAME, FIRST' form, which is still the licensee's name on the register, so confidence is not higher. |
| entity.trading_name | `CRED_LIC_BN` | strip, split, strip, collapse_ws | 0.85 | 158/500 | 0.0 | Registered business names (e.g. BEEWIZE HOME LOAN, PORTFOLIOCARE) that differ from the licensee name. Populated in about 34% of rows. Split on '~' defensively in case of multiple names, one observation each. |
| entity.abn | `CRED_LIC_ABN_ACN` | strip, null_if, digits_only, length_in, abn | 0.9 | 452/500 | 0.0 | Mixed ABN-or-ACN column. 11-digit values are routed to ABN and checksum-validated (89.3% of values pass). '0' is a placeholder for no number and is nulled. Values failing the checksum are dropped, not emitted. |
| entity.acn | `CRED_LIC_ABN_ACN` | strip, null_if, digits_only, length_in, zfill, acn | 0.85 | 42/500 | 0.0 | 9-digit values (about 8% of rows) are ACNs. Some arrive with a dropped leading zero (e.g. '50601093' is 8 digits), so 8-digit values are zero-padded to 9 before the ACN checksum. Length is restricted to 8 or 9 so shorter values are not padded into false matches. |
| entity.entity_type | `CRED_LIC_NAME` | strip, collapse_ws, entity_type_from_name | 0.7 | 500/500 | 0.0 | Deterministic from unambiguous legal suffixes only. Everything else, including individual names, stays unknown. We do not infer sole_trader, trust or partnership. |
| address.locality | `CRED_LIC_LOCALITY` | strip, collapse_ws | 0.9 | 500/500 | 0.0 | Suburb or locality, 100% populated, upper-case as published. |
| address.state | `CRED_LIC_STATE` | strip, state | 0.95 | 500/500 | 0.0 | 8 distinct values, all state-like (NSW, VIC, QLD, WA, SA and so on). |
| address.postcode | `CRED_LIC_PCODE` | strip, postcode | 0.9 | 500/500 | 0.0 | 4-digit AU postcodes, 100% postcode-like. Leading zeros are preserved in the file (e.g. 0800) and the transform would restore any that were dropped. |
| address.country | const `AU` | - | 0.9 | 500/500 | 0.0 | All 8 distinct states are Australian states or territories (no NZ or OTHER values observed). The register is an Australian regulator's. |
| observation.valid_from | `CRED_LIC_START_DT` | strip, date | 0.7 | 500/500 | 0.0 | Licence start date (dd/mm/yyyy, 100% populated). It says when the licence, and so the licensee's listing on the register, began. It does not date every field in the record, e.g. the address. |
| observation.valid_to | `CRED_LIC_END_DT` | strip, date | 0.7 | 0/500 | 0.0 | Licence end date. It is empty in all 4313 rows of this snapshot, so every claim reads as current. The mapping is kept in case later snapshots populate it. |

## Unmapped source fields (8)

- `﻿REGISTER_NAME`: Constant 'Credit Licence' (1 distinct value), no canonical field. The header starts with a UTF-8 BOM, so the engine should strip it or match on the BOM-prefixed name. No mapping uses this column.
- `CRED_LIC_STATUS`: Values are APPR (4306) and SUSP (7). They describe the licence (approved or suspended), not the entity's registration status. Mapping them to entity.status would conflate the two, e.g. a suspended licence is not a suspended company. Left unmapped for the reviewer to decide.
- `CRED_LIC_AFSL_NUM`: AFSL number: '0' in 4003 rows, otherwise equal to the licence number. It is a licence identifier, not ABN, ACN or any canonical business identifier.
- `CRED_LIC_STATUS_HISTORY`: Only 0.6% populated. Free text mixing a suspension reason, dates and '~' separators in an unconfirmed layout. Parsing it into valid_from/valid_to would be a guess.
- `CRED_LIC_LAT`: Geocoded latitude, no canonical field in this ontology subset. The ACN checksum pattern hit on this column is noise.
- `CRED_LIC_LNG`: Geocoded longitude, no canonical field in this ontology subset.
- `CRED_LIC_EDRS`: External dispute resolution scheme membership (AFCA, COSL, FOS), '~'-separated. No canonical field.
- `CRED_LIC_AUTHORISATIONS`: Long free-text description of licence authorisations with '~' separators. No canonical field and not a business identifier.

## Canonical fields left empty (7)

- `entity.status`: Source gives licence status, not entity status. See CRED_LIC_STATUS above.
- `entity.nzbn`: Not present. This is an Australian register.
- `entity.website`: Not present.
- `entity.industry_code`: Not present. All records are credit licensees, but that is not an ANZSIC code.
- `entity.date_registered`: CRED_LIC_START_DT is the licence start, not the entity's registration date, so it goes to observation.valid_from instead.
- `address.full`: No street address, only locality, state and postcode. Synthesising a 'full' address from them would be misleading.
- `observation.observed_at`: No per-record statement date. The engine will use the resource last-modified date (weekly snapshot).

## What the agent changed while revising

- Round from v1 (passed before: True): no changes, signed off

## Preview: unseen records, raw → canonical

**Record 1**

- entity.legal_name: `THE UNIQUE REAL ESTATE & MORTGAGE COMPANY PTY LTD` → `['THE UNIQUE REAL ESTATE & MORTGAGE COMPANY PTY LTD']`
- entity.trading_name: `` → `[]`
- entity.abn: `42160437016` → `['42160437016']`
- entity.acn: `42160437016` → `[]`
- entity.entity_type: `THE UNIQUE REAL ESTATE & MORTGAGE COMPANY PTY LTD` → `['company']`
- address.locality: `ERSKINE PARK` → `['ERSKINE PARK']`
- address.state: `NSW` → `['NSW']`
- address.postcode: `2759` → `['2759']`
- address.country: `AU` → `['AU']`
- observation.valid_from: `20/11/2012` → `['2012-11-20']`
- observation.valid_to: `` → `[]`

**Record 2**

- entity.legal_name: `ADA HOME LOANS PTY LTD` → `['ADA HOME LOANS PTY LTD']`
- entity.trading_name: `` → `[]`
- entity.abn: `96979654` → `[]`
- entity.acn: `96979654` → `['096979654']`
- entity.entity_type: `ADA HOME LOANS PTY LTD` → `['company']`
- address.locality: `SANS SOUCI` → `['SANS SOUCI']`
- address.state: `NSW` → `['NSW']`
- address.postcode: `2219` → `['2219']`
- address.country: `AU` → `['AU']`
- observation.valid_from: `05/12/2010` → `['2010-12-05']`
- observation.valid_to: `` → `[]`

**Record 3**

- entity.legal_name: `RATESEEKER PTY LTD` → `['RATESEEKER PTY LTD']`
- entity.trading_name: `` → `[]`
- entity.abn: `91616470930` → `['91616470930']`
- entity.acn: `91616470930` → `[]`
- entity.entity_type: `RATESEEKER PTY LTD` → `['company']`
- address.locality: `SYDNEY` → `['SYDNEY']`
- address.state: `NSW` → `['NSW']`
- address.postcode: `2000` → `['2000']`
- address.country: `AU` → `['AU']`
- observation.valid_from: `05/02/2018` → `['2018-02-05']`
- observation.valid_to: `` → `[]`

**Record 4**

- entity.legal_name: `MORTGAGE PROCUREMENT SERVICES PTY LTD` → `['MORTGAGE PROCUREMENT SERVICES PTY LTD']`
- entity.trading_name: `MORTGAGE PROCUREMENT SERVICES` → `['MORTGAGE PROCUREMENT SERVICES']`
- entity.abn: `71123331840` → `['71123331840']`
- entity.acn: `71123331840` → `[]`
- entity.entity_type: `MORTGAGE PROCUREMENT SERVICES PTY LTD` → `['company']`
- address.locality: `SUBIACO` → `['SUBIACO']`
- address.state: `WA` → `['WA']`
- address.postcode: `6008` → `['6008']`
- address.country: `AU` → `['AU']`
- observation.valid_from: `17/01/2011` → `['2011-01-17']`
- observation.valid_to: `` → `[]`

**Record 5**

- entity.legal_name: `CAPITAL CONTACTS PTY LTD` → `['CAPITAL CONTACTS PTY LTD']`
- entity.trading_name: `CAPITAL CONTACTS COMMERCIAL FINANCE` → `['CAPITAL CONTACTS COMMERCIAL FINANCE']`
- entity.abn: `13061449232` → `['13061449232']`
- entity.acn: `13061449232` → `[]`
- entity.entity_type: `CAPITAL CONTACTS PTY LTD` → `['company']`
- address.locality: `ADELAIDE` → `['ADELAIDE']`
- address.state: `SA` → `['SA']`
- address.postcode: `5000` → `['5000']`
- address.country: `AU` → `['AU']`
- observation.valid_from: `15/09/2010` → `['2010-09-15']`
- observation.valid_to: `` → `[]`

## Agent notes

- The ABN and ACN mappings share CRED_LIC_ABN_ACN and are routed by length. About 1.6% of rows hold the placeholder '0' (mostly individuals) and a further ~1% fail the checksum. These get no abn or acn observation, which is expected.
- ACN is not derived from the last 9 digits of an 11-digit ABN. That is true for companies but not for sole traders or trusts, and we do not want an unverified identifier asserted as an L0 claim.
- Roughly 10% of CRED_LIC_NAME values have no company suffix, including individuals in 'SURNAME, FIRST' format (e.g. TADROS, EMAD). They are kept as legal_name because it is the register's licensee name, but entity_type stays unknown.
- Licence status is deliberately not mapped to entity.status. If the reviewer accepts that licence status is an acceptable proxy, APPR -> active and SUSP -> suspended is the obvious mapping with default unknown.
- This is a weekly point-in-time snapshot of current licences. CRED_LIC_END_DT is empty throughout, so ceased licences are absent rather than flagged. Absence must not be read as deregistration.
- The licence is CC BY 3.0 AU, taken from the dataset metadata, and attribution to ASIC is required.
- CRED_LIC_AUTHORISATIONS is long and contains '~' separators, so the CSV must be read with proper quote handling. The sample was truncated, so row well-formedness across the whole file is unconfirmed.

---
Approve: `python -m firmable review asic-credit-licensee --approve --reviewer <name>`  
Reject: `python -m firmable review asic-credit-licensee --reject --note "why"`  
Send back with feedback: `python -m firmable review asic-credit-licensee --feedback "what to change"`
