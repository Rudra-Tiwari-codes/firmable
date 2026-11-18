# Review: Notifiable Invoices Register

- **Publisher:** ACT Government Open Data
- **Licence:** Other
- **Resource:** https://www.data.act.gov.au/api/v3/views/kzmf-7uhp/query.json?accessType=DOWNLOAD
- **Agent says it is:** The ACT Government Notifiable Invoices Register: a Socrata JSON export listing each invoice of $25,000 or more paid by an ACT Territory entity, with the supplier's name and ABN, the reporting entity, a description, and the invoice-received and payment dates and amount.
- **Record grain:** one record = one notifiable invoice payment (a supplier paid by a reporting entity on a payment date). Suppliers repeat across rows, so this is not one row per business.
- **Contains business entities:** True (The file is a top-level JSON list of 127,437 flat objects. Each has supplier_name (e.g. 'MARSH PTY LTD') and supplier_abn (e.g. '86 004 651 512'), so supplier businesses are identifiable. reporting_entity (e.g. 'ACTIA') names the government payer. The remaining fields are invoice facts: publish_description, date_invoice_received, payment_date and payment_amount. The ':id', ':version', ':created_at' and ':updated_at' fields are Socrata row metadata.)
- **Reader:** `{"format": "json", "encoding": "utf-8"}`
- **Record id:** `{"strategy": "fields", "fields": [":id"], "rationale": "Socrata row id: unique in all 5000 profiled rows, and the only per-row key in the source. Row grain is one invoice payment. Reviewer should confirm it persists across re-downloads; if it does not, switch to row_hash."}`
- **Source reliability (agent's proposal):** 0.8 (Statutory ACT Government publication under the Government Procurement Act, and ABNs are largely checksum-valid. Supplier names are free text from agency finance systems and are not guaranteed to match the ABR registered name, so this is not higher.)
- **Validation on 500 unseen records:** PASSED
- **Agent cost:** 3 calls, 26705 in / 8933 out tokens, $0.1263, 62.7s, 1 revise round(s)

**Agent's concerns:**

- The declared and sniffed formats agree (JSON). The root is already a list, so no records_path is needed, and there are no title rows or multiple sheets.
- The grain is invoices, not entities. The same supplier appears many times, and each row is an observation of a supplier-government relationship, not a registry entry. Deduplicate and resolve downstream, not at ingest.
- supplier_abn is formatted with spaces ('86 004 651 512'). It needs digits_only and the ABN checksum, and some values may be blank or invalid.
- The dates are ISO-like strings with a T00:00:00.000 suffix and no timezone. payment_amount is a string. Neither is a canonical field in the ontology subset.
- The licence is listed as 'Other', which is unclear, so record it as unknown and flag it for review.
- Socrata ':id' is a row identifier that may be stable and usable as source_record_id. Verify it is unique and persists across re-downloads before relying on it.
- The file is 55 MB and has 127k rows, so the extractor should stream it. The description is truncated, so some caveats about date_invoice_received (missing for some entities) may be hidden.
- Records do not say whether a supplier is a company, sole trader or other. entity_type must stay unknown unless it is derived from an unambiguous suffix.

## Mappings

| canonical | source | transforms | conf | holdout coverage | error rate | rationale |
|---|---|---|---|---|---|---|
| entity.legal_name | `supplier_name` | strip, collapse_ws | 0.55 | 500/500 | 0.0 | Only 56.5% of values carry a company suffix. Others such as 'ADACAS', 'FINITY CONSULTING' and 'ARJOHUNTLEIGH' look like trading names or short forms, so this is ambiguous between registered and trading name. collapse_ws is needed because some values contain embedded newlines (e.g. 'AMERICAN EXPRESS AUSTRALIA\nLIMITED'). |
| entity.abn | `supplier_abn` | strip, digits_only, abn | 0.95 | 498/500 | 0.002 | Spaced 11-digit ABNs ('86 004 651 512'), 99.5% pass the real checksum. The ~0.5% that fail are dropped as parse errors, not emitted. |
| entity.entity_type | `supplier_name` | strip, collapse_ws, entity_type_from_name | 0.8 | 500/500 | 0.0 | Derived only from unambiguous legal suffixes (PTY LTD, LIMITED, INC), otherwise unknown. Names without a suffix stay unknown, which is the ontology default. No other column states entity type. |

## Unmapped source fields (9)

- `:version`: Socrata row-version metadata, not a claim about the business.
- `:created_at`: Socrata load timestamp, not when the publisher stated the claim. It is the same value for ~99.8% of rows, so it is no use as observed_at.
- `:updated_at`: Socrata row update timestamp, not a per-record statement time. Left to the engine's resource last-modified default for observed_at.
- `reporting_entity`: The government payer, not an attribute of the supplier. Captured as a relationship instead.
- `publish_description`: Free-text spend category or description of the purchase (e.g. 'CONTRACTORS'). It is not an ANZSIC industry code and does not describe the supplier's industry.
- `payment_date`: Date the invoice was paid. It does not say when the supplier's name or ABN became true or stopped being true, so it must not feed valid_from/valid_to. It is also not the time the source stated the claim, since publication follows up to ~21 days after month end. No canonical field in the subset fits.
- `date_invoice_received`: Invoice event date, empty for ~32% of rows. It is not a validity date for any entity claim.
- `payment_amount`: Transaction amount (string). No canonical field in the ontology subset.
- `contract_number`: Contract reference, populated in only 0.5% of rows. No canonical field; could support linking to the ACT Contracts Register later.

## Canonical fields left empty (15)

- `entity.acn`: No ACN column. Deriving it from the last 9 ABN digits is unreliable for non-companies (sole traders, trusts, associations), so it is not derived here.
- `entity.nzbn`: Not present; this is an ACT source.
- `entity.trading_name`: Cannot tell which supplier_name values are trading names. Left unmapped to avoid duplicating the ambiguous name into both fields.
- `entity.status`: Receiving a payment says nothing about registration status. Default is unknown.
- `entity.website`: Not present.
- `entity.industry_code`: Not present; publish_description is a spend category, not ANZSIC.
- `entity.date_registered`: Not present.
- `address.full`: No supplier address in the source.
- `address.locality`: No supplier address in the source.
- `address.state`: No supplier address in the source.
- `address.postcode`: No supplier address in the source.
- `address.country`: No address data. The ontology default AU applies; no constant is asserted here since supplier location is not stated.
- `observation.observed_at`: No per-record column states when the source made the claim. The engine falls back to the resource's last-modified date.
- `observation.valid_from`: No column says when the supplier claim became true.
- `observation.valid_to`: No column says when the supplier claim stopped being true.

## Relationship hints

- `reporting_entity` (other): ACT Government entity that paid the supplier (e.g. AHD, ACTIA). This is a customer/payer relationship, not a parent or agent of the supplier. It is a short code or name, not a registry-resolved entity.

## What the agent changed while revising

- Round from v1 (passed before: True): no changes, signed off

## Preview: unseen records, raw → canonical

**Record 1**

- entity.legal_name: `BOC LIMITED` → `['BOC LIMITED']`
- entity.abn: `95 000 029 729` → `['95000029729']`
- entity.entity_type: `BOC LIMITED` → `['company']`

**Record 2**

- entity.legal_name: `AUSTRALIAN ELECTORAL COMMISSION` → `['AUSTRALIAN ELECTORAL COMMISSION']`
- entity.abn: `21 133 285 851` → `['21133285851']`
- entity.entity_type: `AUSTRALIAN ELECTORAL COMMISSION` → `['unknown']`

**Record 3**

- entity.legal_name: `B BRAUN AUSTRALIA PTY LTD` → `['B BRAUN AUSTRALIA PTY LTD']`
- entity.abn: `56 002 945 155` → `['56002945155']`
- entity.entity_type: `B BRAUN AUSTRALIA PTY LTD` → `['company']`

**Record 4**

- entity.legal_name: `ALCOHOL TOBACCO & OTHER DRUG ASSOCIATION` → `['ALCOHOL TOBACCO & OTHER DRUG ASSOCIATION']`
- entity.abn: `50 515 216 820` → `['50515216820']`
- entity.entity_type: `ALCOHOL TOBACCO & OTHER DRUG ASSOCIATION` → `['unknown']`

**Record 5**

- entity.legal_name: `DELOITTE` → `['DELOITTE']`
- entity.abn: `75 490 121 060` → `ERROR ABN '75490121060' fails checksum`
- entity.entity_type: `DELOITTE` → `['unknown']`

## Agent notes

- Grain is invoice payments, not businesses. Suppliers repeat (e.g. one hospital supplier has 550 of 5000 profiled rows). Expect many observations per supplier; deduplicate and resolve downstream, not at ingest.
- supplier_name has 569 distinct values against 468 distinct ABNs, so the same ABN appears under several name variants. Treat the ABN as the stronger identifier and the name as a lower-confidence claim.
- Licence in the dataset metadata is 'Other', which is unclear. Record observation.licence as 'unknown' and flag it for review; check the ACT Open Data terms before downstream use.
- Several profiler pattern hits are coincidences, not signals: payment_amount 'acn_checksum_valid' 0.1%, publish_description 'date_like' 3.5%, contract_number 'postcode_like' 4%. None of these columns are mapped.
- The dataset description is truncated, so caveats about date_invoice_received and other fields may be missing. No mapping depends on them.
- File is ~55 MB / 127k rows. The root is a JSON list, so no records_path is needed; the extractor should stream it.
- Fields supplier_name and supplier_abn were 100% populated in the profiled sample. If blank ABNs appear in the full file, they simply produce no abn observation.

---
Approve: `python -m firmable review kzmf-7uhp --approve --reviewer <name>`  
Reject: `python -m firmable review kzmf-7uhp --reject --note "why"`  
Send back with feedback: `python -m firmable review kzmf-7uhp --feedback "what to change"`
