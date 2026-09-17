# Implementation Plan Quality Evaluation Report

**Plan:** `docs/superpowers/plans/2026-09-11-bayes-limitup-plan.md`
**Date:** 2026-09-11
**Mode:** Innovation
**Grade:** C — 75/100

---

## I. Red Line Results

| Red Line | Status | Details |
|---|---|---|
| R1 REQ Coverage | ⬜ N/A | Innovation mode — no upstream REQ docs exist |
| R2 Placeholder | ✅ Pass | No TBD/TODO/"implement later"/"fill in details" found anywhere in plan |
| R3 Test Case | ✅ Pass | All 8 tasks have Test Cases sections before checkbox steps |

> No red line violations. Grade is determined by weighted scoring.

---

## II. Scoring Overview

| Dimension | Weight | Score | Deductions |
|---|---|---|---|
| Upstream Info Fidelity | 0% | N/A | (skipped — Innovation mode) |
| Executability & Precision | 40% | 60/100 | ISSUE-001 (-40, Critical) |
| TDD Completeness | 30% | 92/100 | ISSUE-006 (-8, Warning) |
| Cross-Task Consistency & Standards | 20% | 84/100 | ISSUE-002 (-8, Warning), ISSUE-003 (-8, Warning) |
| Structure Compliance | 10% | 84/100 | ISSUE-004 (-8, Warning), ISSUE-005 (-8, Warning) |
| **Weighted Total** | **100%** | **75** | |

**Layer Summary:**

| Layer | Total Items | Pass | Violations | N/A (mode) | Pass Rate |
|---|---|---|---|---|---|
| Layer 1: Structure (STR) | 7 | 5 | 0P + 2W | 1 | 83% |
| Layer 2: Traceability (TRC) | 9 | 0 | 0 | 9 | N/A |
| Layer 3: Executability (EXE) | 10 | 10 | 1C | 0 | 100%* |
| Layer 4: Consistency (CNS) | 7 | 5 | 0P + 2W | 1 | 83% |
| Layer 5: TDD (TDD) | 5 | 4 | 0P + 1W | 0 | 80% |
| **Total** | **38** | **24** | **1C + 5W** | **11** | **86%** |

\* EXE pass rate is 100% for items, but ISSUE-001 (Critical) within EXE dimension causes the score to drop to 60 via deduction.

---

## III. Issue List (by Severity)

### 🚨 Critical

None.

### ❌ Error

**ISSUE-001** — Test Assertion Value Bug in Task 3
- **Document:** `2026-09-11-bayes-limitup-plan.md`
- **Location:** Task 3 (build_kline_features integration), Step 1, test code line `assert out["pct"].iloc[3]`
- **Rule:** PLAN-EXE-06 / PLAN-EXE-09 — assertions must be verifiably correct
- **Quote:** `assert out["pct"].iloc[3] == pytest.approx((12 - 10) / 10 * 100 / (70 - 1) * 3)`
- **Description:** The test creates close prices via `np.linspace(10, 12, 70)` (evenly spaced). The assertion expects the value `(12-10)/10 * 100 / 69 * 3 ≈ 0.3478` at index 3. However, `pct_change()` at index 3 computes `(close[3] - close[2]) / close[2] * 100 ≈ (10.1014 - 10.0580) / 10.0580 * 100 ≈ 0.432%`. The expected value 0.3478 does not match the actual pct_change value ~0.432. This assertion **will fail at execution**, blocking Task 3.
- **Severity:** Critical
- **Fix Direction:** Change the expected value to match the actual `pct_change` computation at index 3 for `np.linspace(10, 12, 70)`. The correct expected value is `(linspace[3] - linspace[2]) / linspace[2] * 100`, or refactor the assertion to use a direct formula like `pytest.approx((close[3]/close[2] - 1) * 100)`.
- **Deduction:** [-40 points] (Dimension: Executability & Precision)

### ⚠️ Warning

**ISSUE-002** — No Exception Test Cases
- **Document:** `2026-09-11-bayes-limitup-plan.md`
- **Location:** All tasks Test Cases tables (Tasks 1–8)
- **Rule:** PLAN-CNS-04 — each task's test cases should cover normal + boundary + exception scenario types
- **Description:** Across 30 total test cases, distribution is: 24 normal, 4 boundary, 2 regression, 0 exception. No task includes an exception test case (e.g., invalid input, empty DataFrame, missing required columns). Functions like `board_limit_threshold()`, `add_base_columns()`, `compute_window_features()`, `fit_gaussian()` all have implicit error paths that are untested.
- **Severity:** Warning
- **Fix Direction:** Add at least one exception test case per unit-testable task (e.g., `board_limit_threshold("")` → default; `add_base_columns(empty_df)` → raises or returns empty; `fit_gaussian(X_mismatched_dims)` → raises ValueError).
- **Deduction:** [-8 points] (Dimension: Consistency & Standards)

**ISSUE-003** — Unused Import in Task 1 Test
- **Document:** `2026-09-11-bayes-limitup-plan.md`
- **Location:** Task 1, Step 1, test code import block
- **Rule:** PLAN-CNS-02 — code style consistency; unused imports are lint violations
- **Quote:** `from stock_selector.features.base_columns import (add_base_columns, board_limit_threshold, is_limit_up,)`
- **Description:** `is_limit_up` is imported but never called in any test function. Test case BC-05 describes testing `is_limit_up` but the actual test function `test_is_limit_up_boundary` calls it directly — the import is used. On re-verification: `is_limit_up` IS used in `test_is_limit_up_boundary`. **Retracted** — no actual issue here.
- **Severity:** Warning → **Retracted** (no deduction)

**ISSUE-003 (revised)** — Test Table vs Code Key Name Mismatch
- **Document:** `2026-09-11-bayes-limitup-plan.md`
- **Location:** Task 4, Test Cases table row MD-04
- **Rule:** PLAN-EXE-08 — test case rows should be internally consistent
- **Quote:** Table says "返回 metrics 含 `auc`/`avg_precision`/`prec_at_50_100_200`/`base_rate`/`n_pos_valid`" but test code checks `prec_at_tops` (dict, not flat keys `prec_at_50_100_200`)
- **Description:** The test case table describes flat metric keys (`prec_at_50_100_200`), but the actual test code and implementation use a nested dict `prec_at_tops`. The test code is internally consistent with the implementation, but the table description is misleading. A reader relying on the table would expect different key names.
- **Severity:** Warning
- **Fix Direction:** Update the MD-04 table row to say `prec_at_tops(dict)` instead of `prec_at_50_100_200`, matching the actual code.
- **Deduction:** [-8 points] (Dimension: Consistency & Standards)

**ISSUE-004** — Conventions Block Missing Categories
- **Document:** `2026-09-11-bayes-limitup-plan.md`
- **Location:** Conventions block (lines 11–17)
- **Rule:** PLAN-STR-02 — Conventions block should cover all relevant categories
- **Description:** DB, API patterns, and config categories are not documented. While these may not apply to this ML training pipeline project (no DB writes, no API endpoints, config not used), they are not explicitly marked as "N/A — not applicable" which creates ambiguity.
- **Severity:** Warning
- **Fix Direction:** Add explicit "N/A" markers for DB/API/config categories, or note they are not applicable to this project scope.
- **Deduction:** [-8 points] (Dimension: Structure Compliance)

**ISSUE-005** — Conventions Source Credibility
- **Document:** `2026-09-11-bayes-limitup-plan.md`
- **Location:** Conventions block header
- **Rule:** PLAN-STR-03 — conventions should declare specific codebase files sampled
- **Description:** Says "learned from codebase" but does not list which files were sampled. The reference plan (`2026-09-07-news-llm-analysis-two-layer.md`) follows the same pattern (also no file list), so this is a systemic gap. Specific files that should be cited: `scripts/build_kline_features.py`, `tests/conftest.py`, `stock_selector/backtest/store.py`.
- **Severity:** Warning
- **Fix Direction:** Add a parenthetical listing sampled files: "(learned from codebase: build_kline_features.py, tests/conftest.py, backtest/store.py)"
- **Deduction:** [-8 points] (Dimension: Structure Compliance)

**ISSUE-006** — No Explicit REFACTOR Phase in Complex Tasks
- **Document:** `2026-09-11-bayes-limitup-plan.md`
- **Location:** Task 2 (window.py), Task 4 (modeling.py)
- **Rule:** PLAN-TDD-03 — complex tasks should have a REFACTOR step
- **Description:** Task 2 implements 12 rolling-window feature calculations with pandas/numpy — a complex module deserving a refactor step (e.g., extract helper functions, simplify window logic). Task 4 implements time-split evaluation — moderately complex. Neither has an explicit Step 5 "REFACTOR" step.
- **Severity:** Warning
- **Fix Direction:** Add a REFACTOR step (optional but recommended) after GREEN for Tasks 2 and 4, e.g., "Step X: Refactor — extract `_compute_single_feature()` helper if window.py exceeds 80 lines."
- **Deduction:** [-8 points] (Dimension: TDD Completeness)

---

## IV. Improvement Suggestions (Prioritized)

1. **[Critical]** Fix ISSUE-001: Correct the test assertion in Task 3 Step 1. Replace the wrong expected value `(12-10)/10*100/69*3` with the correct `pct_change` computation for `np.linspace(10, 12, 70)` at index 3.

2. **[Medium]** Fix ISSUE-002: Add at least 1 exception-type test case per unit-testable task (Tasks 1–5). Priority: `board_limit_threshold` edge cases, `add_base_columns` with missing columns, `compute_window_features` with insufficient data.

3. **[Medium]** Fix ISSUE-003: Update MD-04 table row to describe `prec_at_tops(dict)` instead of flat keys `prec_at_50_100_200`.

4. **[Low]** Fix ISSUE-004/005: Mark inapplicable convention categories as "N/A" and cite sampled files.

5. **[Low]** Fix ISSUE-006: Add optional REFACTOR steps for Tasks 2 and 4.

---

## V. Next Steps

- **Grade C (65–79):** Must fix all Error+ issues (ISSUE-001) and re-dispatch `plan-evaluator`.
- ISSUE-001 is Critical — the test assertion will fail at execution, blocking Task 3.
- After fixing ISSUE-001, re-evaluate. Expected score after fix: 75 + 40×0.40 = **91/100 (B grade)**.
- Maximum repair rounds: 2. If still < B after 2 rounds, escalate to user.
