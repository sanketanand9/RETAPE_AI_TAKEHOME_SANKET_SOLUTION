# Settlement Feasibility & Fee Engine — Take-home

## Approach

### Overall structure

`evaluate_offer` is the single entry point. It:

1. Determines the payment cadence (monthly dates from `first_payment_date`, capped at horizon).
2. Tries to build a valid schedule, iterating `k` from `max_k` down to 1 (more payments = more time = more cash available, which helps feasibility and fee front-loading).
3. If feasible, returns the schedule. If not, binary-searches for the minimum lump sum and monthly increment.

### Money and rounding

All money is integer cents throughout. `round_half_up(x)` is implemented as `math.floor(x + 0.5)` — explicitly avoiding Python's default banker's rounding.

### Payment shape builders

Three independent builders, selected by the creditor flags:

**`build_even_payments(k, total, rules)`**
Divides `total` by `k`. Remainder cents go to the *last* `remainder` payments (non-decreasing). Validates each payment against its floor.

**`build_balloon_payments(k, total, rules)`**
Calls `_compute_floors` to get per-position floors, assigns the first `k-1` payments their floor, and lets the final payment absorb the remainder. Validates that the balloon is ≥ the last floor and ≥ the preceding payment (non-decreasing).

**`build_staircase_payments(k, total, rules)`**
Calls `_compute_floors`, checks the floor sum doesn't exceed `total`, then delegates directly to `_fit_segments`.

Both balloon and staircase share `_compute_floors`, which walks positions 1..k tracking token-pay exhaustion and tier step-ups.

### `_fit_segments` — the staircase core

Given per-position floors, a total, and a segment cap, it finds a non-decreasing integer sequence with at most `max_seg` distinct values that sums to `total` and respects all floors.

**Strategy:** recursively try every split point for the first segment boundary. The first segment is set to the maximum floor in its range (the minimum valid uniform level). The rest is solved recursively with `max_seg - 1` segments. Among all valid candidates, pick the one with the lowest first-segment level (objective: keep early payments minimal to free up cash for the program fee).

For `max_seg = 1`, all payments must be equal — `total` must be divisible by `k`, otherwise `None` is returned (infeasible for this `k`).

For `max_seg >= k`, each payment can be its own segment — assign floors and put all surplus on the last payment.

### Program fee allocation (front-loading)

`allocate_program_fee` simulates the account day by day using `_build_cash_maps` (a shared helper that builds credits/debits maps from committed ledger entries dated after `as_of_date`, plus any injected extra credits). On each cadence date, after paying the creditor and bank fee, it collects as much program fee as the remaining balance allows (`min(fee_remaining, balance)`). This greedily front-loads the fee without ever going negative.

### Feasibility simulation

`simulate` reuses `_build_cash_maps`, adds the new schedule debits on top, then walks all dates in chronological order applying credits before debits (same-day ordering), and tracks whether the balance ever goes negative.

### Part 2 — minimum additional funds

Both searches use binary search:

- **Lump sum**: placed on the earliest future draft date (earliest date > `as_of_date` with a credit entry). Earlier placement is weakly more useful (more time for the balance to compound). Binary search over `[1, offer_total + program_fee + max_bank_fees + 1]`.
- **Monthly increment**: added to every future draft (all credit entries dated after `as_of_date`). Binary search over the same range.

For each candidate amount, `is_feasible_with` injects the extra credits and re-runs the full schedule search.

---

## Payment shape interpretation

### Even (`even_pays = true`)

All payments are as equal as possible. When `offer_total % k != 0`, the remainder cents go to the *latest* payments (so the sequence is non-decreasing: `[base, base, ..., base+1, base+1]`). The `k` chosen is the largest that fits within the horizon and produces a feasible schedule — more payments spread the creditor cost thinner, leaving more room for the program fee early on.

### Balloon (`is_ballooning_allowed = true`)

The first `k-1` payments are set to their minimum floor (the maximum of `min_payment_cents`, any applicable tier, and the token-pay rule). The final payment absorbs the entire remaining balance. This is the most aggressive front-loading of the fee: early payments are as small as the rules allow, maximising the cash available for the program fee in early months.

**Token pays and tiers with balloon:** token-pay exhaustion is tracked across the early payments. If `max_token_pays` is reached before the last payment, subsequent early payments must exceed the base minimum. Tier floors apply at their stated positions regardless of shape. The balloon itself must be ≥ the last applicable floor and ≥ the preceding payment (non-decreasing).

### Staircase (neither flag set)

Payments step up over time using at most `max_segments` distinct levels. The `_fit_segments` algorithm keeps the first segment as low as possible (objective), which naturally defers larger creditor payments to later months and frees up early cash for the program fee.

**Step placement:** the algorithm tries every possible split point for the first segment boundary and picks the one that minimises the first-segment level. This means the step-up happens as late as possible — the schedule stays at the floor for as long as it can, then jumps to whatever level is needed to hit the exact sum.

**`max_segments = 1`:** all payments must be equal (same as even, but without the remainder-distribution rule — `total` must be exactly divisible by `k`).

---

## Assumptions

1. **`offer.current_balance_cents` is the creditor balance.** The JSON files use `current_balance_cents` for both the client SDA balance and the creditor balance. The loader maps the offer field to `Offer.current_balance_cents`, and `offer_total_cents` uses that. The ASSIGNMENT note about renaming to `creditor_balance_cents` is acknowledged — the loader and model are unchanged from the provided scaffold.

2. **Lump sum date = earliest future draft date.** The spec says "a date of your choosing (≤ horizon)" and "an earlier lump is weakly more useful." I place it on the first credit entry dated after `as_of_date`, which is the earliest moment cash can arrive.

3. **`k` selection for even pays.** I pick the largest `k` that is feasible. More payments spread the creditor cost thinner, which generally helps fee front-loading. If multiple `k` values are feasible, the largest one is returned.

4. **Fee-only dates.** The spec allows cadence dates that carry only program fee (no creditor payment, no bank fee). In practice, my implementation collects fee on creditor-payment dates first (greedily). Fee-only dates would only appear if the fee couldn't be fully collected during the creditor-payment window — which is possible but rare given the front-loading objective.

5. **`max_segments` ignored for even and balloon.** Per the spec: "Ignored when `even_pays` or `is_ballooning_allowed` is set."

6. **Binary search upper bound.** The upper bound for both Part 2 searches is `offer_total + program_fee + max_k * bank_fee + 1`. This is a safe over-estimate of the maximum shortfall.

---

## Known edge cases and limitations

- **`max_segments = 1` with indivisible total:** `_fit_segments` returns `None` for this `k`, and the engine tries smaller `k` values. If no `k` works, the offer is infeasible.
- **Tier floors that exceed the surplus:** if tiers push the floor sum above `offer_total`, all `k` values return `None` and the offer is infeasible.
- **Token-pay exhaustion mid-balloon:** handled — the floor computation tracks `token_pays_used` across early payments.
- **No future drafts:** if all ledger credits are on or before `as_of_date`, the monthly increment is reported as 0 with `within_guardrail=False` and a descriptive reason.
- **EOM cadence:** correctly handled by `monthly_payment_dates` from the provided scaffold — a Jan 31 start produces Feb 28/29, Mar 31, etc.
- **Same-day credits and debits:** credits are always applied before debits on the same date, as required.
- **Performance:** binary search is O(log N) iterations, each running the full schedule search (O(k²) for `_fit_segments` in the worst case). For the problem sizes here (k ≤ 12), this is fast. For very large `k` or many segments, a DP approach would be more efficient.
