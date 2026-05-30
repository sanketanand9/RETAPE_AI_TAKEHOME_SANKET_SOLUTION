"""Settlement Feasibility & Fee Engine.

evaluate_offer(client, offer, rules) -> Result
See ASSIGNMENT.md for the full specification.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from feasibility.models import (
    Client, CreditorRules, Offer,
    default_first_payment_date, monthly_payment_dates,
    offer_total_cents, program_fee_cents,
)


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ScheduleRow:
    date: date
    creditor_payment_cents: int
    program_fee_cents: int
    bank_fee_cents: int
    balance_cents: int


@dataclass
class FundsOption:
    amount_cents: int
    within_guardrail: bool
    reason: str
    date: date | None = None       # lump-sum only
    num_drafts: int | None = None  # monthly-increment only


@dataclass
class AdditionalFunds:
    lump_sum: FundsOption
    monthly_increment: FundsOption


@dataclass
class Result:
    feasible: bool
    pay_shape_used: str | None = None
    schedule: list[ScheduleRow] | None = None
    additional_funds: AdditionalFunds | None = None

    def to_dict(self) -> dict:
        out: dict = {"feasible": self.feasible, "pay_shape_used": self.pay_shape_used}
        out["schedule"] = (
            [{"date": r.date.isoformat(), "creditor_payment_cents": r.creditor_payment_cents,
              "program_fee_cents": r.program_fee_cents, "bank_fee_cents": r.bank_fee_cents,
              "balance_cents": r.balance_cents} for r in self.schedule]
            if self.schedule is not None else None
        )
        if self.additional_funds is None:
            out["additional_funds"] = None
        else:
            def _opt(o: FundsOption) -> dict:
                d = {"amount_cents": o.amount_cents, "within_guardrail": o.within_guardrail, "reason": o.reason}
                if o.date is not None:
                    d["date"] = o.date.isoformat()
                if o.num_drafts is not None:
                    d["num_drafts"] = o.num_drafts
                return d
            out["additional_funds"] = {"lump_sum": _opt(self.additional_funds.lump_sum),
                                       "monthly_increment": _opt(self.additional_funds.monthly_increment)}
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def round_half_up(x: float) -> int:
    """Round-half-up (0.5 rounds away from zero). Python's round() uses banker's rounding."""
    return math.floor(x + 0.5)


def _floor_at(pos: int, rules: CreditorRules, token_pays_used: int) -> int:
    """Minimum allowed creditor payment at 1-based position pos."""
    floor = rules.min_payment_cents
    for from_pay, tier_min in rules.min_payment_tiers:
        if pos >= from_pay:
            floor = max(floor, tier_min)
    if token_pays_used >= rules.max_token_pays:
        floor = max(floor, rules.min_payment_cents + 1)
    return floor


def _compute_floors(k: int, rules: CreditorRules) -> list[int]:
    """Return the floor for each of k payment positions."""
    floors, token_pays_used = [], 0
    for i in range(k):
        f = _floor_at(i + 1, rules, token_pays_used)
        floors.append(f)
        if f == rules.min_payment_cents:
            token_pays_used += 1
    return floors


# ---------------------------------------------------------------------------
# Payment builders
# ---------------------------------------------------------------------------

def build_even_payments(k: int, total: int, rules: CreditorRules) -> list[int] | None:
    """k equal (or as-equal-as-possible) payments. Remainder on latest (non-decreasing)."""
    base, rem = divmod(total, k)
    payments = [base] * (k - rem) + [base + 1] * rem
    token_pays_used = 0
    for i, p in enumerate(payments):
        if p < _floor_at(i + 1, rules, token_pays_used):
            return None
        if p == rules.min_payment_cents:
            token_pays_used += 1
    return payments


def build_balloon_payments(k: int, total: int, rules: CreditorRules) -> list[int] | None:
    """k-1 payments at floor, last payment absorbs remainder (balloon)."""
    floors = _compute_floors(k, rules)
    if k == 1:
        return [total] if total >= floors[0] else None
    early = floors[:-1]
    balloon = total - sum(early)
    if balloon < max(early[-1], floors[-1]):
        return None
    return early + [balloon]


def _fit_segments(floors: list[int], total: int, max_seg: int) -> list[int] | None:
    """Fit payments into at most max_seg uniform-level segments, non-decreasing, sum=total.

    Each payment[i] >= floors[i]. Minimises the first segment level (front-load fee).
    """
    k = len(floors)
    if k == 0:
        return [] if total == 0 else None

    # Enough segments for each payment to be its own level
    if max_seg >= k:
        payments = list(floors)
        surplus = total - sum(payments)
        if surplus < 0:
            return None
        payments[-1] += surplus
        return payments if all(payments[i] >= payments[i-1] for i in range(1, k)) else None

    # All payments must be the same level
    if max_seg == 1:
        if total % k != 0:
            return None
        L = total // k
        return [L] * k if all(L >= f for f in floors) else None

    # Try every split point for the first segment; recurse on the rest
    best: list[int] | None = None
    for j in range(1, k):
        level_A = max(floors[:j])
        remaining = total - j * level_A
        if remaining < sum(floors[j:]):
            continue
        rest = _fit_segments(floors[j:], remaining, max_seg - 1)
        if rest is None or rest[0] < level_A:
            continue
        candidate = [level_A] * j + rest
        if sum(candidate) == total and (best is None or candidate[0] < best[0]):
            best = candidate

    # Also try all-same level if divisible
    if total % k == 0:
        L = total // k
        if all(L >= f for f in floors):
            candidate = [L] * k
            if best is None or candidate[0] < best[0]:
                best = candidate

    return best


def build_staircase_payments(k: int, total: int, rules: CreditorRules) -> list[int] | None:
    """Staircase: at most max_segments distinct levels, early payments as low as possible."""
    floors = _compute_floors(k, rules)
    if sum(floors) > total:
        return None
    return _fit_segments(floors, total, rules.max_segments)


# ---------------------------------------------------------------------------
# Simulation and fee allocation
# ---------------------------------------------------------------------------

def _build_cash_maps(
    client: Client,
    extra_credits: list[tuple[date, int]] | None,
) -> tuple[dict[date, int], dict[date, int]]:
    """Build credits/debits maps from committed ledger entries (after as_of_date)."""
    credits: dict[date, int] = defaultdict(int)
    debits: dict[date, int] = defaultdict(int)
    for e in client.ledger:
        if e.date > client.as_of_date:
            (credits if e.type == "credit" else debits)[e.date] += e.amount_cents
    if extra_credits:
        for d, amt in extra_credits:
            credits[d] += amt
    return credits, debits


def allocate_program_fee(
    client: Client,
    payment_dates: list[date],
    creditor_pmts: list[int],
    bank_fees: list[int],
    total_fee: int,
    extra_credits: list[tuple[date, int]] | None = None,
) -> list[int] | None:
    """Greedily collect program fee as early as possible (after creditor + bank fee)."""
    credits, debits = _build_cash_maps(client, extra_credits)
    all_dates = sorted(set(list(credits) + list(debits) + payment_dates))
    payment_set = set(payment_dates)

    balance = client.current_balance_cents
    fee_remaining = total_fee
    fee_schedule: dict[date, int] = {d: 0 for d in payment_dates}

    for d in all_dates:
        balance += credits.get(d, 0)
        if d in payment_set:
            idx = payment_dates.index(d)
            balance -= creditor_pmts[idx] + bank_fees[idx]
            if balance < 0:
                return None
            collected = min(fee_remaining, balance)
            fee_schedule[d] = collected
            balance -= collected
            fee_remaining -= collected
        else:
            balance -= debits.get(d, 0)
        if balance < 0:
            return None

    return [fee_schedule[d] for d in payment_dates] if fee_remaining == 0 else None


def simulate(
    client: Client,
    payment_dates: list[date],
    creditor_pmts: list[int],
    prog_fees: list[int],
    bank_fees: list[int],
    extra_credits: list[tuple[date, int]] | None = None,
) -> tuple[bool, list[int]]:
    """Simulate SDA balance. Returns (feasible, balance_at_each_payment_date)."""
    credits, debits = _build_cash_maps(client, extra_credits)
    for i, d in enumerate(payment_dates):
        debits[d] += creditor_pmts[i] + bank_fees[i] + prog_fees[i]

    balance = client.current_balance_cents
    feasible = True
    bal_at: dict[date, int] = {}

    for d in sorted(set(list(credits) + list(debits))):
        balance += credits.get(d, 0)
        balance -= debits.get(d, 0)
        if balance < 0:
            feasible = False
        if d in set(payment_dates):
            bal_at[d] = balance

    return feasible, [bal_at.get(d, 0) for d in payment_dates]


# ---------------------------------------------------------------------------
# Schedule attempt
# ---------------------------------------------------------------------------

def _try_schedule(
    client: Client, offer: Offer, rules: CreditorRules,
    k: int, dates: list[date],
    extra_credits: list[tuple[date, int]] | None = None,
) -> tuple[list[int], list[int], list[int], list[int]] | None:
    """Try to build a valid k-payment schedule. Returns (creditor, fees, bank, balances) or None."""
    total = offer_total_cents(offer)
    total_fee = program_fee_cents(offer, rules)

    if rules.even_pays:
        creditor_pmts = build_even_payments(k, total, rules)
    elif rules.is_ballooning_allowed:
        creditor_pmts = build_balloon_payments(k, total, rules)
    else:
        creditor_pmts = build_staircase_payments(k, total, rules)

    if creditor_pmts is None:
        return None

    bank_fees = [rules.bank_fee_cents] * k
    prog_fees = allocate_program_fee(client, dates, creditor_pmts, bank_fees, total_fee, extra_credits)
    if prog_fees is None:
        return None

    feasible, balances = simulate(client, dates, creditor_pmts, prog_fees, bank_fees, extra_credits)
    return (creditor_pmts, prog_fees, bank_fees, balances) if feasible else None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def evaluate_offer(client: Client, offer: Offer, rules: CreditorRules) -> Result:
    """Evaluate a settlement offer. See ASSIGNMENT.md for the full specification."""
    horizon = client.last_draft_date
    first_pay = offer.first_payment_date or default_first_payment_date(client)
    max_k = min(rules.max_payments, rules.max_terms)
    shape = "even" if rules.even_pays else ("balloon" if rules.is_ballooning_allowed else "staircase")

    # Part 1: find the largest feasible k
    for k in range(max_k, 0, -1):
        dates = [d for d in monthly_payment_dates(first_pay, k) if d <= horizon]
        if len(dates) < k:
            continue
        result = _try_schedule(client, offer, rules, k, dates)
        if result is not None:
            creditor_pmts, prog_fees, bank_fees, balances = result
            schedule = [
                ScheduleRow(dates[i], creditor_pmts[i], prog_fees[i], bank_fees[i], balances[i])
                for i in range(k)
            ]
            return Result(feasible=True, pay_shape_used=shape, schedule=schedule)

    # Part 2: infeasible — find minimum additional funds
    future_drafts = sorted(
        {e.date for e in client.ledger if e.date > client.as_of_date and e.type == "credit"}
    )

    def is_feasible_with(extra: list[tuple[date, int]]) -> bool:
        for k in range(max_k, 0, -1):
            dates = [d for d in monthly_payment_dates(first_pay, k) if d <= horizon]
            if len(dates) < k:
                continue
            if _try_schedule(client, offer, rules, k, dates, extra_credits=extra) is not None:
                return True
        return False

    # Lump sum: binary search, placed on earliest future draft date
    lump_date = future_drafts[0] if future_drafts else first_pay
    lo, hi = 1, offer_total_cents(offer) + program_fee_cents(offer, rules) + rules.bank_fee_cents * max_k + 1
    while lo < hi:
        mid = (lo + hi) // 2
        if is_feasible_with([(lump_date, mid)]):
            hi = mid
        else:
            lo = mid + 1
    lump_amount = lo
    lump_limit = round_half_up(0.65 * offer_total_cents(offer))
    lump_ok = lump_amount <= lump_limit
    lump_reason = "" if lump_ok else f"Lump sum {lump_amount} exceeds guardrail limit {lump_limit}"

    # Monthly increment: binary search over all future drafts
    n = len(future_drafts)
    if n == 0:
        inc_amount, inc_ok, inc_reason = 0, False, "No future drafts to increment"
    else:
        lo2, hi2 = 1, offer_total_cents(offer) + program_fee_cents(offer, rules) + 1
        while lo2 < hi2:
            mid2 = (lo2 + hi2) // 2
            if is_feasible_with([(d, mid2) for d in future_drafts]):
                hi2 = mid2
            else:
                lo2 = mid2 + 1
        inc_amount = lo2
        inc_limit = max(10000, round_half_up(0.40 * client.draft_amount_cents))
        inc_ok = inc_amount <= inc_limit
        inc_reason = "" if inc_ok else f"Monthly increment {inc_amount} exceeds guardrail limit {inc_limit}"

    return Result(
        feasible=False,
        additional_funds=AdditionalFunds(
            lump_sum=FundsOption(lump_amount, lump_ok, lump_reason, date=lump_date),
            monthly_increment=FundsOption(inc_amount, inc_ok, inc_reason, num_drafts=n),
        ),
    )
