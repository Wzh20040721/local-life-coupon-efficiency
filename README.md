# Local-Life Coupon Subsidy Efficiency Analysis

## TL;DR

This project reconstructs order-level economics, links issued coupons to observed transactions, segments coupon performance, identifies subsidy pools for audit, and models cost-saving scenarios.

The analysis identifies **CNY 809.9k of subsidy spend for priority audit**. If randomized holdout tests confirm that 10%–30% of this pool can be removed without reducing GMV, the platform could save **CNY 81.0k–243.0k over six months**, while improving GMV per subsidy yuan from 19.69 to **20.05–20.83**, an efficiency gain of approximately **1.9%–5.8%**.

It also finds **21.29 million matured coupons with no matched transaction**. These coupons generated no directly observed subsidy payout. If the marginal delivery and contact cost is CNY 0.01–0.10 per coupon, avoiding comparable low-return contacts represents an additional **CNY 212.9k–2.13M sensitivity range**. This is a scenario, not booked savings; the model must be replaced with actual channel costs.

## Business Question

The project evaluates whether coupon distribution is economically efficient and answers four practical questions:

1. How much GMV and subsidy spend occurred at the true order grain?
2. Which matured coupons and coupon batches produced no traceable transaction?
3. Which realized subsidy costs deserve immediate audit?
4. How much cost reduction and efficiency improvement may be achievable under explicit assumptions?

## What Was Done

- Reconstructed one row per order from discount-component transaction records.
- Counted user payment once per order and summed all subsidy components.
- Linked issued coupons to transaction records using `Coupon_id`.
- Applied maturity and right-censoring rules using the June 30, 2023 cutoff.
- Segmented results by business unit, user activity, coupon amount, and spending threshold.
- Identified an auditable subsidy pool using transparent business rules.
- Built contact-cost and subsidy-reduction sensitivity scenarios.
- Exported deterministic JSON and CSV evidence for every reported result.

## Statistical and Analytical Methods

### 1. Data-quality profiling and reconciliation

The workflow checks source row counts, missing values, duplicate identifiers, order-level consistency, and coupon-status conflicts. Independent DuckDB queries recompute the main totals to verify that the reporting pipeline and the reconciliation path agree.

### 2. Order-grain aggregation

The transaction file contains multiple discount components for some orders. Order economics are therefore calculated as:

```text
Order GMV = one observed Actual_pay value + sum of Reduce_amount components
```

The data contains no conflicting `Actual_pay` values within the same order, which validates this aggregation rule.

### 3. Deterministic coupon-to-transaction linkage

An issued coupon is considered transaction-matched only when its `Coupon_id` appears in the transaction data. This avoids treating the coupon-status field as the sole source of truth when status and transaction evidence disagree.

### 4. Maturity and right-censoring controls

Coupons ending on or before June 30, 2023 are considered mature. Coupons ending later are right-censored and excluded from the matured no-transaction population.

### 5. Stratified descriptive analysis

Weighted counts and rates are compared across business units, activity segments, coupon-value bands, and spending-threshold bands. These comparisons identify high-priority test populations, but are not interpreted as causal effects.

### 6. Sensitivity analysis

Two scenario families are evaluated:

- contact-cost savings across CNY 0.01, 0.05, and 0.10 per low-return coupon contact;
- subsidy savings from removing 10%, 20%, or 30% of the auditable subsidy pool while holding GMV constant.

The second assumption must be validated through randomized holdout experiments before the result can be treated as achievable savings.

## Main Results

| Metric | Result |
|---|---:|
| Orders | 1,999,688 |
| GMV | CNY 86.78M |
| Actual subsidy | CNY 4.41M |
| Subsidy rate | 5.08% |
| GMV per subsidy yuan | 19.69 |
| Matured coupons with no matched transaction | 21,285,084 |
| Zero-transaction coupon batches | 1,081,736 |
| Auditable subsidy pool | CNY 809,928.70 |
| Auditable pool share of subsidy | 18.37% |

Business unit E is the clearest test priority: its subsidy rate is 23.53%, and each subsidy yuan corresponds to only CNY 4.25 of observed GMV, compared with CNY 19.69 platform-wide.

## Recommended Real-World Actions

1. Pause repeat distribution for the 1,939 zero-transaction batches containing at least 100 matured coupons, while retaining a small randomized holdout.
2. Trace campaign and channel ownership for CNY 660.5k of subsidies on orders with no coupon identifier.
3. Prioritize business unit E and orders where subsidy is at least as large as user payment.
4. Run user-level randomized tests comparing the current policy with 10% and 20% subsidy reductions.
5. Optimize for incremental contribution margin divided by subsidy and contact cost—not redemption rate alone.
6. Move budget only after tests show no unacceptable loss in orders, retention, contribution margin, or customer experience.

## Validation

- Five automated tests cover order grain, coupon maturity, transaction attribution, missing status, scenario arithmetic, deterministic artifacts, and cutoff validation.
- The complete 3.7GB source dataset was processed end to end.
- Independent queries exactly reconcile orders, GMV, subsidy, audit-pool value, coupon counts, transaction matches, and matured unmatched coupons.
- Raw user-level CSV files, local environments, caches, and credentials are excluded from version control.

## Reproduce the Analysis

Python 3.9 or newer is required.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python analysis_full.py \
  --data-dir . \
  --output-dir artifacts \
  --cutoff 2023-06-30
```

The local data directory must contain:

- `①合并画像_232375490_20260123_encrypted.csv`
- `②活跃_232401619_20260123.csv`
- `③获券_232388348_20260123.csv`
- `④交易数据_234065457_20260126.csv`

The raw files total approximately 3.7GB and are intentionally excluded from GitHub.

## Repository Structure

```text
analysis_full.py                 Reproducible full-data analysis
tests/test_analysis_full.py      Analytical contract and integration tests
最终报告.md                       Detailed business report in Chinese
artifacts/results.json           Core metrics and sensitivity scenarios
artifacts/run_manifest.json      Scope and metric definitions
artifacts/tables/                Auditable aggregate result tables
```

## Interpretation Limits

The dataset supports reliable accounting, matching, segmentation, and scenario analysis. It does not reveal the counterfactual outcome for users who received coupons. Consequently, matched GMV and GMV per subsidy yuan are observational efficiency measures—not incremental ROI. Causal savings must be established with randomized holdouts and contribution-margin data.
