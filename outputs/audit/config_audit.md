# Config audit

Generated 2026-11-06 by `python -m firmable audit`: every config over its whole file, no model calls. "Beyond window" is rows past the first 5,000, which the agent runs behind these configs never read.

| Source | Rows | Agent window covers | Worst error rate (whole file) | Worst error rate (beyond window) | ABNs with unrelated names | Names under several ABNs | Licence flag | Source changed |
|---|---|---|---|---|---|---|---|---|
| acnc-register | 66,541 | 7.5% | 0.002 | 0.002 | 0 of 65,930 | 402 of 65,136 | none | no |
| asic-credit-licensee | 4,313 | 100.0% | 0.0 | no such rows | 0 of 3,851 | 1 of 3,850 | none | no |
| kzmf-7uhp | 127,437 | 3.9% | 0.001 | 0.001 | 609 of 5,142 | 535 of 5,735 | licence id 'other' is not a recognised open licence; read the terms before using this source | no |
| nsw-liquor-licence-premises-list-as-at-8-feb-2021 | 18,747 | 26.7% | 0.0 | 0.0 | not checked | not checked | none | no |
| qbcc-licensed-contractors-register | 206,226 | 2.4% | 0.009 | 0.009 | 2,175 of 56,832 | 130 of 59,085 | none | no |
| system_procurement_contract-disclosure-report | 126 | 100.0% | 0.037 | no such rows | 1 of 70 | 0 of 74 | none | no |

## Warnings

**acnc-register**

- 215 rows needed a #n suffix to keep record ids unique.

**asic-credit-licensee**

- entity.acn (from CRED_LIC_ABN_ACN): 91% of non-empty cells produce no value and no error. Confirm that is intended filtering.

**kzmf-7uhp**

- 609 of 5142 ABNs carry names that look unrelated. A valid checksum does not prove the ABN belongs to the name in that row. Either the pairing is unreliable, or several parties legitimately share an ABN (e.g. employees licensed under an employer). Check which.
- 535 of 5735 names appear under more than one ABN.

**nsw-liquor-licence-premises-list-as-at-8-feb-2021**

- entity.legal_name (from Licensee): 66% of non-empty cells produce no value and no error. Confirm that is intended filtering.
- entity.entity_type (from Licensee): 66% of non-empty cells produce no value and no error. Confirm that is intended filtering.
- 3 rows needed a #n suffix to keep record ids unique.

**qbcc-licensed-contractors-register**

- 2175 of 56832 ABNs carry names that look unrelated. A valid checksum does not prove the ABN belongs to the name in that row. Either the pairing is unreliable, or several parties legitimately share an ABN (e.g. employees licensed under an employer). Check which.
- 1 rows needed a #n suffix to keep record ids unique.

**system_procurement_contract-disclosure-report**

- none

