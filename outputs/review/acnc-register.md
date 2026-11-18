# Review: ACNC Registered Charities

- **Publisher:** Australian Charities and Not-for-profits Commission (ACNC)
- **Licence:** Creative Commons Attribution 3.0 Australia
- **Resource:** https://data.gov.au/data/dataset/b050b242-4487-4306-abf5-07ca073e5594/resource/8fb32972-24e9-4c95-885e-7140be51be8a/download/datadotgov_main.csv
- **Agent says it is:** The ACNC Charity Register extract: a comma-delimited CSV with a single header row, listing Australian registered charities with ABN, legal name, other names, address, website, registration and establishment dates, size, states of operation, and Y-flag columns for purposes and beneficiaries. The declared format (CSV) matches the sniffed format (delimited text).
- **Record grain:** one record = one registered charity, identified by ABN. The sample rows show no duplicate ABNs, but uniqueness across the full file is not checked here.
- **Contains business entities:** True (The header starts with ABN, Charity_Legal_Name and Other_Organisation_Names. Sample rows have 11-digit ABNs and registered names such as 'CLARK COURT LTD', 'HOPE ARABIC CHURCH INCORPORATED' and 'TECHNION AUSTRALIA PBI LIMITED'. They also have street addresses with state and postcode, and websites. The description says it is a subset of the ACNC Charity Register. The comma delimiter count is consistently 68 and no title or notes rows sit above the header.)
- **Reader:** `{"format": "csv", "encoding": "utf-8", "delimiter": ",", "header_row": 0}`
- **Record id:** `{"strategy": "fields", "fields": ["ABN", "Charity_Legal_Name"], "rationale": "ABN is the register key (every non-empty value passes the checksum and the top values are all unique), but 1.6% of rows have no ABN and ~2 non-empty ABNs may repeat across 5000 rows. Adding the legal name keeps blank-ABN rows distinct and separates any duplicate ABNs. The cost is that the id changes if a charity renames itself between weekly snapshots. Held-out check: 0 duplicate ids in 500 records."}`
- **Source reliability (agent's proposal):** 0.9 (National regulator and authoritative for charity registration, names and ABNs. Addresses, websites and size flags are charity self-reported and can be stale or sloppy. Withheld records are absent by design.)
- **Validation on 500 unseen records:** PASSED
- **Agent cost:** 6 calls, 85121 in / 34925 out tokens, $0.4866, 206.5s, 3 revise round(s)

**Agent's concerns:**

- Quoted cells contain commas (for example Other_Organisation_Names and Operating_Countries 'AUS, KHM'), so the reader must honour CSV quoting.
- Other_Organisation_Names contains non-Latin text (Arabic), so the encoding must be UTF-8. Check the BOM and that the characters decode cleanly.
- Dates look like dd/mm/yyyy (13/11/2024, 28/06/2024), which is day-first. Registration_Date and Date_Organisation_Established need an explicit format. Financial_Year_End is a day-month value like '30-Jun', not a full date.
- Many cells are empty. Charity_Size is blank on some rows, and the Y-flag columns are sparse.
- Postcodes and ABNs must be read as strings so leading zeros are preserved. Do not let the reader infer numeric types.
- The file is a weekly-updated snapshot. The row-level observation time is not in the data, so the last-modified date is the likely fallback.
- The description says charities can have information withheld, so the register is incomplete by design.
- The description is truncated, so it may contain a further caveat I could not read.
- The licence is CC BY 3.0 AU, which requires attribution. Record it in the envelope.
- The Address_Type column shows 'Business' on all sample rows, but it may take other values elsewhere in the file.
- Some sample names are not obviously registered legal entities (for example 'Frederick Miller Community Garden'). That is a concern for entity_type, not for reading the file.

## Mappings

| canonical | source | transforms | conf | holdout coverage | error rate | rationale |
|---|---|---|---|---|---|---|
| entity.abn | `ABN` | strip, abn | 0.97 | 488/500 | 0.0 | 11-digit values, 100% pass the real ABN checksum; the header and description identify it as the charity's ABN. |
| entity.legal_name | `Charity_Legal_Name` | strip, collapse_ws | 0.92 | 499/500 | 0.0 | Registered charity name as held on the ACNC register; 72% carry company suffixes (Ltd, Inc, Pty Ltd). Case is left as the source gives it. |
| entity.entity_type | `Charity_Legal_Name` | entity_type_from_name | 0.7 | 500/500 | 0.0 | No entity type column. Derived only from unambiguous legal suffixes in the registered name (Pty Ltd / Limited / Inc); everything else stays unknown. Trusts, Aboriginal Corporations, 'Community Garden' style names etc. are deliberately not guessed. |
| entity.website | `Charity_Website` | strip, url | 0.65 | 338/500 | 0.0 | Self-reported website; 88% of non-empty values are URL-like. Some are social media pages, and some point to a parent or related organisation's site rather than the charity's own (e.g. BHL Dalyellup Two Apartments Ltd lists bethanie.com.au). |
| address.full | `Address_Line_1 + Address_Line_2 + Address_Line_3 + Town_City + State + Postcode` | collapse_ws, strip | 0.75 | 437/500 | 0.0 | Concatenation of the structured address parts. Address_Line_1 sometimes holds a C/O or organisation name rather than a street, and Line_3 has stray values like 'via' or 'Australia', so the full string is only moderately reliable. |
| address.locality | `Town_City` | strip, collapse_ws | 0.9 | 430/500 | 0.0 | Suburb or town values (Sydney, Carlton North, Wyoming). |
| address.state | `State` | strip, state | 0.92 | 434/500 | 0.0 | 100% of non-empty values are state-like abbreviations (NSW, VIC, QLD...). Held-out: 434/434 parsed. |
| address.postcode | `Postcode` | strip, postcode | 0.9 | 434/500 | 0.002 | Postcode-like values; read as string; the postcode transform restores a dropped leading zero. A rare invalid value (e.g. 6 digits) is dropped (1/435 on held-out). |
| address.country | `Country` | strip, collapse_ws, upper, map_values | 0.8 | 439/500 | 0.0 | Mostly 'Australia' / 'AUSTRALIA'. Other literal country names are mapped to ISO codes rather than defaulting to AU, which would be wrong for them. Unmapped values (e.g. the phone number '0419032014') are dropped, not defaulted. |

## Unmapped source fields (59)

- `Other_Organisation_Names`: Mixed content: other/former names, abbreviations, 'Previously known as' text and non-Latin translations. Commas occur inside single names (e.g. 'ATF ...'), so a comma split would corrupt them, and it is unclear that they are trading names. Mapping it to trading_name would risk wrong claims.
- `Address_Type`: Constant 'Business' on all 5000 records; no canonical field and no information.
- `Registration_Date`: Date the charity registered with the ACNC. This is not the company or entity registration date (date_registered), and it is not when the name or address claims became true (valid_from applies to every emitted observation), so it is left unmapped to avoid a misleading mapping. Reviewer may override. Format is dd/mm/yyyy (%d/%m/%Y).
- `Date_Organisation_Established`: Establishment date of the organisation. Not clearly the registration date in the ontology sense, and it is often identical to Registration_Date. Format is dd/mm/yyyy.
- `Charity_Size`: ACNC size classification (Small/Medium/Large) based on revenue; no canonical field. Blank on 14%.
- `Number_of_Responsible_Persons`: Count of responsible persons; no canonical field.
- `Financial_Year_End`: Day-month value like '30-Jun', not a date; no canonical field.
- `Operates_in_ACT`: State-of-operation flag; no canonical field.
- `Operates_in_NSW`: State-of-operation flag; no canonical field.
- `Operates_in_NT`: State-of-operation flag; no canonical field.
- `Operates_in_QLD`: State-of-operation flag; no canonical field.
- `Operates_in_SA`: State-of-operation flag; no canonical field.
- `Operates_in_TAS`: State-of-operation flag; no canonical field.
- `Operates_in_VIC`: State-of-operation flag; no canonical field.
- `Operates_in_WA`: State-of-operation flag; no canonical field.
- `Operating_Countries`: ISO3 list of operating countries ('AUS, KEN'); not the address country.
- `PBI`: Public benevolent institution flag; no canonical field.
- `HPC`: Health promotion charity flag; no canonical field.
- `Preventing_or_relieving_suffering_of_animals`: Charitable purpose Y-flag; no canonical field.
- `Advancing_Culture`: Charitable purpose Y-flag; no canonical field.
- `Advancing_Education`: Charitable purpose Y-flag; no canonical field.
- `Advancing_Health`: Charitable purpose Y-flag; no canonical field.
- `Promote_or_oppose_a_change_to_law__government_poll_or_prac`: Charitable purpose Y-flag; no canonical field.
- `Advancing_natual_environment`: Charitable purpose Y-flag; no canonical field.
- `Promoting_or_protecting_human_rights`: Charitable purpose Y-flag; no canonical field.
- `Purposes_beneficial_to_ther_general_public_and_other_analogous`: Charitable purpose Y-flag; no canonical field.
- `Promoting_reconciliation__mutual_respect_and_tolerance`: Charitable purpose Y-flag; no canonical field.
- `Advancing_Religion`: Charitable purpose Y-flag; no canonical field.
- `Advancing_social_or_public_welfare`: Charitable purpose Y-flag; no canonical field.
- `Advancing_security_or_safety_of_Australia_or_Australian_public`: Charitable purpose Y-flag; no canonical field.
- `Aboriginal_or_TSI`: Beneficiary Y-flag; no canonical field.
- `Adults`: Beneficiary Y-flag; no canonical field.
- `Aged_Persons`: Beneficiary Y-flag; no canonical field.
- `Children`: Beneficiary Y-flag; no canonical field.
- `Communities_Overseas`: Beneficiary Y-flag; no canonical field.
- `Early_Childhood`: Beneficiary Y-flag; no canonical field.
- `Ethnic_Groups`: Beneficiary Y-flag; no canonical field.
- `Families`: Beneficiary Y-flag; no canonical field.
- `Females`: Beneficiary Y-flag; no canonical field.
- `Financially_Disadvantaged`: Beneficiary Y-flag; no canonical field.
- `LGBTIQA+`: Beneficiary Y-flag; no canonical field.
- `General_Community_in_Australia`: Beneficiary Y-flag; no canonical field.
- `Males`: Beneficiary Y-flag; no canonical field.
- `Migrants_Refugees_or_Asylum_Seekers`: Beneficiary Y-flag; no canonical field.
- `Other_Beneficiaries`: Beneficiary Y-flag; no canonical field.
- `Other_Charities`: Beneficiary Y-flag; no canonical field.
- `People_at_risk_of_homelessness`: Beneficiary Y-flag; no canonical field.
- `People_with_Chronic_Illness`: Beneficiary Y-flag; no canonical field.
- `People_with_Disabilities`: Beneficiary Y-flag; no canonical field.
- `Pre_Post_Release_Offenders`: Beneficiary Y-flag; no canonical field.
- `Rural_Regional_Remote_Communities`: Beneficiary Y-flag; no canonical field.
- `Unemployed_Person`: Beneficiary Y-flag; no canonical field.
- `Veterans_or_their_families`: Beneficiary Y-flag; no canonical field.
- `Victims_of_crime`: Beneficiary Y-flag; no canonical field.
- `Victims_of_Disasters`: Beneficiary Y-flag; no canonical field.
- `Youth`: Beneficiary Y-flag; no canonical field.
- `animals`: Y-flag (apparently an additional purpose/activity tag); meaning unclear and no canonical field.
- `environment`: Y-flag (apparently an additional purpose/activity tag); meaning unclear and no canonical field.
- `other_gender_identities`: Beneficiary Y-flag; no canonical field.

## Canonical fields left empty (9)

- `entity.trading_name`: Only candidate is Other_Organisation_Names, which mixes former names, abbreviations and free text and cannot be split safely.
- `entity.acn`: No ACN column. Deriving it from the ABN would give wrong values for the many non-company charities (incorporated associations, trusts, etc.), where the last 9 digits are not an ACN.
- `entity.nzbn`: Not present; Australian source.
- `entity.status`: No status column. The register lists currently registered charities, but that is not stated per record, so status is left as unknown rather than inferred as active.
- `entity.industry_code`: No ANZSIC or industry column; the purpose flags are not an industry classification.
- `entity.date_registered`: Registration_Date is ACNC registration, not entity registration; left unmapped (see unmapped_source_fields).
- `observation.observed_at`: No per-record statement date; the engine will use the resource's last-modified date.
- `observation.valid_from`: No column giving when the emitted name/address claims became true.
- `observation.valid_to`: No end date column; deregistered or revoked charities are not represented.

## What the agent changed while revising

- Round from v1 (passed before: False): 
  - Rebuilt the address.country transform: added an `upper` step, upper-cased the map keys, moved `case_insensitive` inside `args`, and added more ISO mappings (Singapore, New Zealand, and others).: All 439 non-empty Country values failed with 'not in map', including plain 'Australia' and 'AUSTRALIA'. The lower-case keys never matched, and the top-level `case_insensitive` flag was evidently ignored. Upper-casing first and using upper-case keys no longer depends on that flag. Singapore also failed because it was not in the map, so I added common countries. Values still not in the map (junk such as a phone number) are dropped rather than defaulted.
  - Kept the postcode mapping as is.: The single error ('139658', 0.2%) is a genuinely invalid postcode that should be dropped, and it is well under the 5% threshold.
  - Added reviewer notes about the country map and about an anonymised-looking legal name in the preview.: A reviewer should know that countries outside the map are dropped, and that a legal name like '847219-X8N0N9- PAF' is passed through as source-stated with no ABN.
- Round from v2 (passed before: False): 
  - address.country: moved the map_values arguments (map, case_insensitive) to the top level of the transform object, so it is {"op": "map_values", "map": {...}, "case_insensitive": true}. Previously they were nested under an "args" key.: The vocabulary shape is {"op": name, ...args}, so arguments sit beside "op". The engine most likely never saw my nested map and treated it as empty. That fits the error "value 'AUSTRALIA' not in map" on 100% of values, even for 'Australia' and after the upper transform. The round-1 failure had the same symptom, so case sensitivity was probably not the real cause. I kept the upper-cased keys and case_insensitive as a belt-and-braces measure.
  - Lowered entity.website confidence from 0.75 to 0.65 and made the rationale more explicit.: The preview shows BHL DALYELLUP TWO APARTMENTS LTD with the website of its parent, bethanie.com.au. A website column that is sometimes a parent's or related organisation's site is not reliably the entity's own website, so the confidence was too high.
  - Updated the country note in notes to reflect the likely root cause.: The earlier note blamed case sensitivity; the more probable cause was the misplaced argument nesting.
- Round from v3 (passed before: True): no changes, signed off

## Preview: unseen records, raw → canonical

**Record 1**

- entity.abn: `74428878865` → `['74428878865']`
- entity.legal_name: `Ridges Christian Fellowship` → `['Ridges Christian Fellowship']`
- entity.entity_type: `Ridges Christian Fellowship` → `['unknown']`
- entity.website: `ridgeschristianfellowship.org` → `['http://ridgeschristianfellowship.org']`
- address.full: `66 Rifle Range Rd, Wollongbar, NSW, 2477` → `['66 Rifle Range Rd, Wollongbar, NSW, 2477']`
- address.locality: `Wollongbar` → `['Wollongbar']`
- address.state: `NSW` → `['NSW']`
- address.postcode: `2477` → `['2477']`
- address.country: `Australia` → `['AU']`

**Record 2**

- entity.abn: `92686361428` → `['92686361428']`
- entity.legal_name: `BHL DALYELLUP TWO APARTMENTS LTD` → `['BHL DALYELLUP TWO APARTMENTS LTD']`
- entity.entity_type: `BHL DALYELLUP TWO APARTMENTS LTD` → `['company']`
- entity.website: `https://www.bethanie.com.au/` → `['https://www.bethanie.com.au']`
- address.full: `L 4 1060 Hay St, West Perth, WA, 6005` → `['L 4 1060 Hay St, West Perth, WA, 6005']`
- address.locality: `West Perth` → `['West Perth']`
- address.state: `WA` → `['WA']`
- address.postcode: `6005` → `['6005']`
- address.country: `Australia` → `['AU']`

**Record 3**

- entity.abn: `64685869729` → `['64685869729']`
- entity.legal_name: `DISABILITY ENTREPRENEURSHIP AND INNOVATION AUSTRALIA LTD` → `['DISABILITY ENTREPRENEURSHIP AND INNOVATION AUSTRALIA LTD']`
- entity.entity_type: `DISABILITY ENTREPRENEURSHIP AND INNOVATION AUSTRALIA LTD` → `['company']`
- entity.website: `www.deiaustralia.com.au` → `['http://www.deiaustralia.com.au']`
- address.full: `U 5 60 Princes Hwy, Yallah, NSW, 2530` → `['U 5 60 Princes Hwy, Yallah, NSW, 2530']`
- address.locality: `Yallah` → `['Yallah']`
- address.state: `NSW` → `['NSW']`
- address.postcode: `2530` → `['2530']`
- address.country: `Australia` → `['AU']`

**Record 4**

- entity.abn: `` → `[]`
- entity.legal_name: `847219-X8N0N9- PAF` → `['847219-X8N0N9- PAF']`
- entity.entity_type: `847219-X8N0N9- PAF` → `['unknown']`
- entity.website: `` → `[]`
- address.full: `` → `[]`
- address.locality: `` → `[]`
- address.state: `` → `[]`
- address.postcode: `` → `[]`
- address.country: `` → `[]`

**Record 5**

- entity.abn: `57679353103` → `['57679353103']`
- entity.legal_name: `ENCOUNTERCARE LIMITED` → `['ENCOUNTERCARE LIMITED']`
- entity.entity_type: `ENCOUNTERCARE LIMITED` → `['company']`
- entity.website: `www.encountercare.com.au` → `['http://www.encountercare.com.au']`
- address.full: `77 Girrawheen Ave, Girrawheen, WA, 6064` → `['77 Girrawheen Ave, Girrawheen, WA, 6064']`
- address.locality: `Girrawheen` → `['Girrawheen']`
- address.state: `WA` → `['WA']`
- address.postcode: `6064` → `['6064']`
- address.country: `Australia` → `['AU']`

## Agent notes

- Licence: Creative Commons Attribution 3.0 Australia. Attribution to ACNC is required; carry it in the envelope.
- Parse as UTF-8 CSV with quoting honoured. Other_Organisation_Names contains Arabic text and embedded commas. Read every column as string so ABN and postcode leading zeros survive.
- Dates are day-first dd/mm/yyyy (%d/%m/%Y) if a reviewer later maps Registration_Date or Date_Organisation_Established. Financial_Year_End is a day-month value, not a date.
- About 1.6% of rows have no ABN, and about 12% have no address. These emit fewer observations; the abn transform drops values that fail the checksum, but all sampled ABNs were valid.
- entity_type_from_name is a suffix heuristic on the legal name. Many charities (churches, community groups, Aboriginal Corporations, trusts) will come out as unknown. That is intended.
- Country map (round 2 revision): v1 and v2 both failed on 100% of values with "value not in map", even for 'Australia' after upper-casing. The likely cause is that the map arguments were nested under an "args" key instead of sitting beside "op". They are now top-level. Countries not in the map are dropped, not defaulted to AU. The map is a finite list, so a small share of overseas countries may be dropped. A reviewer should confirm the engine does not apply the ontology default AU to dropped values, since that would mislabel overseas addresses.
- Some legal names look anonymised or withheld (e.g. '847219-X8N0N9- PAF', with no ABN or address). They are passed through as the source states them; consider a downstream filter.
- Address_Line_1 sometimes holds a C/O or organisation name (e.g. a school P&C), so address.full may not be a clean street address for those rows.
- Charity_Website sometimes holds social media pages or a parent organisation's site rather than the charity's own site (seen in the held-out preview: a subsidiary listing its parent's site).
- The dataset description was truncated, so a caveat about future withdrawal of information may be missing. The file is a weekly snapshot, so the last-modified date is a coarse proxy for observed_at.
- 5000-row profile only: ABN uniqueness across the full file is not verified (4918 distinct over ~4920 non-empty). Held-out 500 records had no duplicate record ids.

---
Approve: `python -m firmable review acnc-register --approve --reviewer <name>`  
Reject: `python -m firmable review acnc-register --reject --note "why"`  
Send back with feedback: `python -m firmable review acnc-register --feedback "what to change"`
