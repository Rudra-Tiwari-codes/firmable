# Review: Liquor licence premises list as at 8 Feb 2021

- **Publisher:** Department of Customer Service
- **Licence:** Creative Commons Attribution 3.0 Australia
- **Resource:** https://data.nsw.gov.au/data/dataset/f4e5fd07-eb6a-420c-a857-6eda213ba6a3/resource/530d86bc-cdc3-4e3f-b828-5106cd0e48c3/download/premises-list-as-at-8-february-2021.csv
- **Agent says it is:** A NSW Liquor & Gaming register of liquor licence premises as at 8 Feb 2021, published by the Department of Customer Service. Each row is one licence for one premises, with licence type, status, start date, trading status, premises name and address, geocoordinates, LGA, licensee name, authorisations and gaming machine counts.
- **Record grain:** one record = one liquor licence (identified by Licence number) at one premises. A licensee can hold many licences, so the same business will appear on multiple rows.
- **Contains business entities:** True (The 'Licensee' column holds registered-style names in upper case (e.g. 'CHARLESTOWN GOLF CLUB LTD', 'CYPRUS COMMUNITY OF N.S.W. LIMITED'). 'Licence name' holds the premises or trading name, which often differs from the licensee (e.g. 'Sporties Tuncurry' vs 'FORSTER-TUNCURRY MEMORIAL SERVICES CLUB LTD'; 'Club Dalmeny' vs 'NAROOMA SPORTING AND SERVICES CLUB LTD'). The file has no ABN or ACN column. Each row has a unique licence number such as LIQC300200019.)
- **Reader:** `{"format": "csv", "encoding": "cp1252", "delimiter": ",", "header_row": 0}`
- **Record id:** `{"strategy": "fields", "fields": ["Licence number", "Address", "Suburb"], "rationale": "Licence number is the natural key (one row = one licence at one premises), but the profile shows 4995 distinct values in 5000 rows, with several appearing twice. The key alone does not identify a raw row. Adding Address and Suburb separates repeats that are the same licence at different premises. The held-out run produced 0 duplicate record ids."}`
- **Source reliability (agent's proposal):** 0.8 (Official NSW regulator register (Liquor & Gaming NSW) and authoritative for licence, licensee and premises claims. The score is below 0.9 because it is a 2021 snapshot with no ABN/ACN, because licensee names are free text, and because some addresses are partial.)
- **Validation on 500 unseen records:** PASSED
- **Agent cost:** 7 calls, 80113 in / 31423 out tokens, $0.4361, 198.2s, 3 revise round(s)

**Agent's concerns:**

- No ABN/ACN/NZBN column, so entity resolution will have to rely on names and addresses, which is a weaker link.
- The licence metadata says CC BY 3.0 AU, but the description text says CC BY 4.0. The two conflict, so the licence should be recorded and flagged for review.
- The file is a snapshot as at 8 Feb 2021 (Last-Modified 9 Feb 2021), while the CKAN metadata_modified is 2025. The per-record observation time should be the 2021 snapshot date, not the 2025 metadata date.
- The 'Authorisation restriction name' column is a comma-joined list inside quotes and appears truncated at about 100 characters in the sample ('Unrestr'). Do not parse it as a list.
- The sample shows no title rows or notes rows, but I have only seen the first rows. Trailing summary rows and encoding oddities (e.g. in names with '&') are not ruled out.
- Postcodes are 4 digits in the sample, and the dataset is NSW-only, so state would be a constant rather than a column. 'Licensee' might be blank or hold individuals' names on some licence types (e.g. hotel or packaged liquor), so the entity type cannot be assumed from this column.
- Some licensees may be individuals (sole traders), so check later whether the Licensee values are all registered company names.

## Mappings

| canonical | source | transforms | conf | holdout coverage | error rate | rationale |
|---|---|---|---|---|---|---|
| entity.legal_name | `Licensee` | strip, collapse_ws, regex_extract | 0.85 | 184/500 | 0.0 | Licensee mixes registered-style names (ending LTD, LIMITED, PTY LTD, INC) and individuals' personal names. Only values with a legal suffix, and without trustee/ATF/trading-as wording, are kept as legal_name. Non-matching values produce no value (confirmed on holdout), so personal names are not recorded as registered names. |
| entity.entity_type | `Licensee` | strip, collapse_ws, regex_extract, entity_type_from_name | 0.8 | 184/500 | 0.0 | Derived only from unambiguous legal suffixes in the Licensee name, and only for values that pass the same filter as legal_name. Individuals and trustee-style values get no entity_type observation, so they stay at the ontology default of unknown. sole_trader is never inferred. |
| entity.trading_name | `Licence name` | strip, collapse_ws | 0.7 | 500/500 | 0.0 | Licence name is the premises or venue name (e.g. 'Royal Hotel', 'Sundowner Hotel'). Some clubs use their legal name here (e.g. 'Inverell East Bowling Club Ltd'), so it is a trading-name claim and not a verified registered name. |
| address.full | `Address + Suburb + Postcode` | collapse_ws, regex_extract | 0.7 | 485/500 | 0.0 | Street line, suburb and postcode joined with ', '. Values containing the literal 'NULL' placeholder (about 3% of rows) produce no value. The street line is partial and has no state, so this is not a complete postal address. |
| address.locality | `Suburb` | strip, null_if, collapse_ws | 0.9 | 485/500 | 0.0 | Suburb is upper-case locality names. 'NULL' is treated as missing. |
| address.postcode | `Postcode` | strip, null_if, postcode | 0.9 | 485/500 | 0.0 | 4-digit postcodes. 'NULL' is treated as missing. |
| address.state | const `NSW` | - | 0.9 | 500/500 | 0.0 | NSW Liquor & Gaming register. Premises are licensed in NSW and the sample shows only NSW localities and 2xxx postcodes. There is no state column, so this is a constant. |
| address.country | const `AU` | - | 0.95 | 500/500 | 0.0 | Australian state government register. |

## Unmapped source fields (27)

- `Licence number`: Used in record_id. It is a licence identifier, not an entity identifier (ABN, ACN or NZBN). About 10% of values pass the ACN checksum, which is chance level, so it is not an ACN.
- `Start date`: Licence start date (dd/mm/yyyy). Mapping it to observation.valid_from would assert that the licensee, entity type and names held from that date. The preview shows 1940s-1980s dates alongside individual licensees, so that claim is likely false. Unmapped rather than risk wrong validity dates. If revisited, use {"op":"date","formats":["%d/%m/%Y"]} with flat args.
- `Licence type`: Licence category (hotel, club, limited, on-premises). It is not an entity type or an industry code.
- `Status`: Status of the licence ('Current'/'Suspended'), not the business's registration status. Mapping it to entity.status would be a guess.
- `Trading Status`: Whether the premises is currently trading. It is not the entity's registration status.
- `Latitude`: No canonical field in this ontology subset. 'NULL' placeholders are present.
- `Longitude`: No canonical field in this ontology subset. 'NULL' placeholders are present.
- `LGA`: No canonical field. 'NULL' placeholders are present.
- `Precinct`: No canonical field. It is sparse.
- `Business type`: Venue-type description (e.g. 'Full hotel', 'Restaurant'), not ANZSIC. It is not mapped to industry_code.
- `Authorisation restriction name`: Comma-joined list, truncated at about 80-100 characters in the source. It is not parsed as a list because truncated values would be wrong. No canonical field.
- `Extended trading`: Licence attribute flag with no canonical field.
- `Reduced trading`: Licence attribute flag with no canonical field.
- `Primary service`: Licence attribute flag with no canonical field.
- `Right to keep gaming`: Licence attribute flag with no canonical field.
- `Unrestricted`: Licence attribute flag with no canonical field.
- `After12am`: Trading hours flag with no canonical field.
- `After3am`: Trading hours flag with no canonical field.
- `After5am`: Trading hours flag with no canonical field.
- `SA2`: Statistical area with no canonical field.
- `Band`: Gaming/licence band with no canonical field.
- `GMEs`: Gaming machine count with no canonical field.
- `PMPs`: Gaming machine count with no canonical field.
- `Auth limit`: Gaming machine count with no canonical field.
- `Unfulfilled Quota`: Gaming machine count with no canonical field.
- `GMT`: Gaming machine count with no canonical field.
- `EGMs`: Gaming machine count with no canonical field.

## Canonical fields left empty (10)

- `entity.abn`: No ABN column in the source.
- `entity.acn`: No ACN column in the source.
- `entity.nzbn`: Not an NZ source and no NZBN column.
- `entity.status`: Only licence and trading status are present, which describe the licence and premises, not the entity. Left as the default of unknown.
- `entity.website`: Not present.
- `entity.industry_code`: No ANZSIC code. Business type is a different classification.
- `entity.date_registered`: Start date is the licence start, not entity registration.
- `observation.observed_at`: No per-record column says when the source stated the claim. The engine will use the resource's last-modified date.
- `observation.valid_from`: Only the licence start date exists, and it does not reliably date the licensee or name claims (see Start date).
- `observation.valid_to`: No licence end or expiry column.

## What the agent changed while revising

- Round from v1 (passed before: False): 
  - entity.legal_name: set regex_extract to required=false: Individuals' names such as 'Sean Henderson' are deliberately not registered names. With required=true the engine counted each one as a parse error (63% error rate). The suffix filter is meant to produce no value for them, not an error. Registered-style values still pass through. Reviewer: on the next run confirm that a non-match yields no value rather than the raw string being passed through. If it passes through, unmap legal_name instead.
  - entity.entity_type: set regex_extract to required=false: Same reason as legal_name. Individuals should get no entity_type observation, so they stay at the ontology default of unknown. sole_trader is still never inferred.
  - observation.valid_from: unmapped Start date: The first fix is that the date transform had its formats nested under 'args', so the engine ignored them and fell back to ['iso'], giving 100% errors. Transform arguments must be flat, e.g. {"op":"date","formats":["%d/%m/%Y"]}. The meaning check then showed that mapping is wrong anyway. Start date is when the licence began, and the preview has 19/07/1948 for a licensee named 'Sean Henderson' and 1945 for 'Nathan Roberts'. A person did not become the licensee in 1948, and valid_from would apply to the legal_name and entity_type claims in the record. A wrong validity date is worse than none, so the column is unmapped. If the engine later supports per-field validity, the licence start could be reconsidered for the trading name or premises claim.
  - Updated notes and unmapped lists: Reflects the valid_from removal, the new required=false behaviour, and the dropped 'trustee for ...' licensees.
- Round from v2 (passed before: True): 
  - address.full: set regex_extract required to false.: The 15 errors (3%) were all 'NULL, NULL, NULL', which is a legitimately missing address and not a parse failure. required=false drops them as 'no value' instead of flagging them as errors. That keeps the error counts meaningful. Locality and postcode behave the same way. The holdout confirmed that required=false on a regex_extract yields no value, not a pass-through.
  - entity.legal_name and entity.entity_type: added a negative lookahead to the suffix regex that excludes values containing 'trustee for', 'as trustee', 'ATF', 'T/AS' or 'trading as'.: The preview shows the Licensee column contains trustee-style values ('The trustee for Merri Merri Creek Trust'). A value such as 'The trustee for Foo Pty Ltd' or 'X ATF Y Pty Ltd' would pass the suffix test and be recorded as a registered name, or as company, for the wrong entity. Under 'a wrong mapping is worse than no mapping' these are now dropped. Some genuine registered names, such as 'Perpetual Trustee Company Limited', are only dropped when they contain the phrases above, so that loss is small. I could not test this on holdout data, so the reviewer should check the dropped-value counts.
  - Updated notes (placeholders, review check, trusts).: The holdout preview confirmed that non-matching Licensee values (e.g. 'Sean Henderson') produce no value and are not passed through, so that review check is resolved. The placeholder note no longer describes the address.full drops as errors. The trust note now describes the new exclusion.
  - Reviewed the other mappings against the preview and left them unchanged.: The legal_name and entity_type values are correct company names and 'company'. Trading name, locality, postcode and the NSW/AU constants match their canonical meaning. Start date, Status, Trading Status and Business type remain unmapped because mapping them would be a guess.
- Round from v3 (passed before: True): no changes, signed off

## Preview: unseen records, raw → canonical

**Record 1**

- entity.legal_name: `Sean Henderson` → `[]`
- entity.entity_type: `Sean Henderson` → `[]`
- entity.trading_name: `Royal Hotel` → `['Royal Hotel']`
- address.full: `Renshaw St, BINNAWAY, 2395` → `['Renshaw St, BINNAWAY, 2395']`
- address.locality: `BINNAWAY` → `['BINNAWAY']`
- address.postcode: `2395` → `['2395']`
- address.state: `NSW` → `['NSW']`
- address.country: `AU` → `['AU']`

**Record 2**

- entity.legal_name: `POTTSVILLE DISTRICT BOWLS & SPORTS CLUB LIMITED` → `['POTTSVILLE DISTRICT BOWLS & SPORTS CLUB LIMITED']`
- entity.entity_type: `POTTSVILLE DISTRICT BOWLS & SPORTS CLUB LIMITED` → `['company']`
- entity.trading_name: `Pottsville District Bowls & Sports Club Limited` → `['Pottsville District Bowls & Sports Club Limited']`
- address.full: `479 Coast Rd, POTTSVILLE, 2489` → `['479 Coast Rd, POTTSVILLE, 2489']`
- address.locality: `POTTSVILLE` → `['POTTSVILLE']`
- address.postcode: `2489` → `['2489']`
- address.state: `NSW` → `['NSW']`
- address.country: `AU` → `['AU']`

**Record 3**

- entity.legal_name: `The trustee for Merri Merri Creek Trust` → `[]`
- entity.entity_type: `The trustee for Merri Merri Creek Trust` → `[]`
- entity.trading_name: `Sundowner Hotel` → `['Sundowner Hotel']`
- address.full: `Tucka Tucka St, QUAMBONE, 2831` → `['Tucka Tucka St, QUAMBONE, 2831']`
- address.locality: `QUAMBONE` → `['QUAMBONE']`
- address.postcode: `2831` → `['2831']`
- address.state: `NSW` → `['NSW']`
- address.country: `AU` → `['AU']`

**Record 4**

- entity.legal_name: `Nathan Roberts` → `[]`
- entity.entity_type: `Nathan Roberts` → `[]`
- entity.trading_name: `Guildford Hotel` → `['Guildford Hotel']`
- address.full: `309 Guildford Rd, GUILDFORD, 2161` → `['309 Guildford Rd, GUILDFORD, 2161']`
- address.locality: `GUILDFORD` → `['GUILDFORD']`
- address.postcode: `2161` → `['2161']`
- address.state: `NSW` → `['NSW']`
- address.country: `AU` → `['AU']`

**Record 5**

- entity.legal_name: `INVERELL EAST BOWLING CLUB LTD` → `['INVERELL EAST BOWLING CLUB LTD']`
- entity.entity_type: `INVERELL EAST BOWLING CLUB LTD` → `['company']`
- entity.trading_name: `Inverell East Bowling Club Ltd` → `['Inverell East Bowling Club Ltd']`
- address.full: `21-29 Glen Innes Rd, INVERELL, 2360` → `['21-29 Glen Innes Rd, INVERELL, 2360']`
- address.locality: `INVERELL` → `['INVERELL']`
- address.postcode: `2360` → `['2360']`
- address.state: `NSW` → `['NSW']`
- address.country: `AU` → `['AU']`

## Agent notes

- LICENCE: CKAN metadata says CC BY 3.0 AU but the description text says CC BY 4.0. Record the CKAN licence value and flag the conflict for human review before use.
- OBSERVED_AT: The data is a snapshot as at 8 Feb 2021 (resource last-modified 9 Feb 2021). CKAN metadata_modified (2025-06-24) is a later metadata edit and must not be used as observed_at. Check that the engine takes the resource's last-modified date.
- NO IDENTIFIERS: There is no ABN, ACN or NZBN, so entity linking must rely on name and address. Expect low link_confidence and do not auto-merge on the licensee name alone.
- LICENSEE MIX: Licensee holds both registered-style names and individuals' personal names. On the 500-record holdout only 184 (37%) have a company suffix. Only suffix-matched values become legal_name or entity_type. Individuals' names are intentionally not emitted, so those records get a trading_name and address but no legal_name. The holdout preview confirmed that non-matching values (e.g. 'Sean Henderson') produce no value and do not pass through.
- TRUSTS: Licensees like 'The trustee for Merri Merri Creek Trust' name a trustee relationship, not a registered entity name. They are dropped by the suffix filter. The regex also has a negative lookahead excluding values containing 'trustee for', 'as trustee', 'ATF', 'T/AS' or 'trading as', so values like 'The trustee for Foo Pty Ltd' are not recorded as a registered name or company. This lookahead was not exercised on the holdout, so reviewer: check the dropped-value counts. No trustee relationship is emitted because the transforms cannot reliably separate the trust from the trustee. The regex also drops legitimate registered names without a listed suffix (e.g. 'CO-OPERATIVE'). Extend the suffix list if needed.
- TRADING NAME: Licence name is sometimes a club's registered name, so trading_name can contain legal-style names. Reconciliation should treat it as a lower-trust name claim.
- VALID_FROM: Deliberately unmapped (see Start date). The engine would apply it to every observation in the record, including the licensee claim, and the preview shows dates decades before the plausible tenure of individual licensees.
- DUPLICATE KEYS: Licence number has 4995 distinct values over 5000 rows, so record_id is a composite. Reviewer: please confirm whether the repeats are exact duplicates or the same licence at different premises.
- PLACEHOLDERS: The literal string 'NULL' is used for missing Address, Suburb, Postcode, Latitude, Longitude and LGA. It is handled for the mapped address fields. About 3% of rows have no usable address and get no address.full value. This is missing data, not a parse error (regex_extract required=false).
- TRANSFORM ARGS: Transform arguments are flat keys on the transform object (e.g. {"op":"date","formats":[...]}), not nested under 'args'.
- Address typos exist in the source (e.g. '68 Bluehaven Dribe'). Values are passed through as stated and not corrected.

---
Approve: `python -m firmable review nsw-liquor-licence-premises-list-as-at-8-feb-2021 --approve --reviewer <name>`  
Reject: `python -m firmable review nsw-liquor-licence-premises-list-as-at-8-feb-2021 --reject --note "why"`  
Send back with feedback: `python -m firmable review nsw-liquor-licence-premises-list-as-at-8-feb-2021 --feedback "what to change"`
