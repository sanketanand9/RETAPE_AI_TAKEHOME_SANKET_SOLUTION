"""Tests for the Settlement Feasibility & Fee Engine.

Covers (per ASSIGNMENT.md §10): even / staircase / balloon shapes; token-pay
and tier floors; max_segments cap; exact-sum; date-by-date simulation (same-day
ordering, balance hitting exactly $0); horizon limit; fee compliance; both Part 2
minima; round-half-up rounding; non-decreasing payments; guardrail pass/fail.
"""

from __future__ import annotations

from datetime import date

from feasibility.engine import evaluate_offer, round_half_up
from feasibility.models import (
    Client, CreditorRules, LedgerEntry, Offer,
    add_months, load_case, offer_total_cents, program_fee_cents,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def make_client(
    draft_amount: int = 20000,
    first_draft: str = "2026-01-01",
    last_draft: str = "2026-06-01",
    balance: int = 0,
    extra_debits: list[tuple[str, int]] | None = None,
) -> Client:
    first, last = date.fromisoformat(first_draft), date.fromisoformat(last_draft)
    ledger: list[LedgerEntry] = []
    d = first
    while d <= last:
        ledger.append(LedgerEntry(d, draft_amount, "credit"))
        d = add_months(d, 1)
    for ds, amt in (extra_debits or []):
        ledger.append(LedgerEntry(date.fromisoformat(ds), amt, "debit"))
    return Client(draft_amount, 1, first, last, date(2025, 12, 31), balance, ledger)


def make_offer(
    creditor_balance: int = 100000,
    original_balance: int = 100000,
    settlement_pct: float = 0.5,
    first_payment_date: str | None = "2026-01-31",
) -> Offer:
    fpd = date.fromisoformat(first_payment_date) if first_payment_date else None
    return Offer("TestCo", creditor_balance, original_balance, settlement_pct, fpd)


def make_rules(
    max_k: int = 6,
    min_payment: int = 2500,
    max_token_pays: int = 6,
    tiers: list[tuple[int, int]] | None = None,
    even_pays: bool = False,
    ballooning: bool = False,
    max_segments: int = 4,
    bank_fee: int = 0,
    program_fee_pct: float = 0.0,
) -> CreditorRules:
    return CreditorRules(max_k, max_k, min_payment, max_token_pays, tiers or [],
                         even_pays, ballooning, max_segments, bank_fee, program_fee_pct)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_round_half_up():
    # 0.5 must round up, not to-even
    assert round_half_up(0.5) == 1
    assert round_half_up(1.5) == 2
    assert round_half_up(1.4) == 1
    assert round_half_up(5.0) == 5


def test_even_shape():
    # shape label, exact sum, non-decreasing, remainder on last payments
    # offer_total = round(0.5 * 100006) = 50003 -> [16667, 16668, 16668] with k=3
    r = evaluate_offer(
        make_client(draft_amount=30000, last_draft="2026-03-01"),
        make_offer(creditor_balance=100006),
        make_rules(max_k=3, min_payment=100, even_pays=True),
    )
    assert r.feasible
    assert r.pay_shape_used == "even"
    pmts = [row.creditor_payment_cents for row in r.schedule]
    assert sum(pmts) == offer_total_cents(make_offer(creditor_balance=100006))
    assert pmts == sorted(pmts)
    assert len(set(pmts)) <= 2  # at most base and base+1


def test_balloon_shape():
    # shape label, last payment is largest, early payments at floor, non-decreasing
    r = evaluate_offer(
        make_client(draft_amount=10000),
        make_offer(creditor_balance=40000),
        make_rules(ballooning=True),
    )
    assert r.feasible
    assert r.pay_shape_used == "balloon"
    pmts = [row.creditor_payment_cents for row in r.schedule]
    assert pmts == sorted(pmts)
    assert pmts[-1] == max(pmts)
    assert all(p == 2500 for p in pmts[:-1])  # early payments at floor


def test_staircase_shape_tiers_and_segments():
    # shape label, max_segments=2 respected, tier floor from position 7,
    # early payments below tier, non-decreasing, exact sum
    client = make_client(draft_amount=10000, last_draft="2027-01-01")
    offer = make_offer(creditor_balance=150000, original_balance=150000, settlement_pct=0.4)
    rules = make_rules(max_k=12, tiers=[(7, 5000)], max_segments=2, bank_fee=500, program_fee_pct=0.2)
    r = evaluate_offer(client, offer, rules)
    assert r.feasible
    assert r.pay_shape_used == "staircase"
    pmts = [row.creditor_payment_cents for row in r.schedule]
    assert pmts == sorted(pmts)
    assert sum(pmts) == offer_total_cents(offer)
    assert len(set(pmts)) <= 2
    assert all(p >= 5000 for p in pmts[6:])   # tier floor from position 7
    assert any(p < 5000 for p in pmts[:6])    # early payments below tier


def test_token_pay_limit():
    # at most max_token_pays payments may sit exactly at min_payment_cents
    r = evaluate_offer(
        make_client(last_draft="2026-12-01"),
        make_offer(),
        make_rules(max_k=10, max_token_pays=2, max_segments=4),
    )
    if r.feasible:
        assert sum(1 for row in r.schedule if row.creditor_payment_cents == 2500) <= 2


def test_max_segments_cap():
    # max_segments=1 forces all payments equal (total must be divisible by k)
    r = evaluate_offer(
        make_client(),
        make_offer(creditor_balance=120000),  # offer_total=60000, divisible by 6
        make_rules(max_segments=1, min_payment=100),
    )
    if r.feasible:
        assert len(set(row.creditor_payment_cents for row in r.schedule)) == 1


def test_simulation_balance_and_same_day_ordering():
    # (a) balance hits exactly $0: draft=10000 Jan 1, single payment Jan 31 = 10000
    r = evaluate_offer(
        make_client(draft_amount=10000, first_draft="2026-01-01", last_draft="2026-02-01"),
        make_offer(creditor_balance=20000, first_payment_date="2026-01-31"),
        make_rules(max_k=1, min_payment=100),
    )
    assert r.feasible
    assert r.schedule[-1].balance_cents == 0

    # (b) same-day ordering: draft=10000 and payment=8000 both on Jan 1
    # credits first -> 0+10000-8000=2000 (feasible); debits first -> negative
    r2 = evaluate_offer(
        make_client(draft_amount=10000, first_draft="2026-01-01", last_draft="2026-01-01"),
        make_offer(creditor_balance=16000, first_payment_date="2026-01-01"),
        make_rules(max_k=1, min_payment=100),
    )
    assert r2.feasible
    assert r2.schedule[0].balance_cents >= 0


def test_balance_never_negative_and_horizon():
    # all provided feasible cases: balance >= 0 everywhere, no payment past horizon
    for case in ["case1_feasible_even", "case3_balloon", "case4_tiers"]:
        client, offer, rules = load_case(f"cases/{case}")
        r = evaluate_offer(client, offer, rules)
        assert r.feasible
        for row in r.schedule:
            assert row.balance_cents >= 0
            assert row.date <= client.last_draft_date


def test_horizon_too_tight():
    # first payment date is past the horizon -> infeasible
    r = evaluate_offer(
        make_client(last_draft="2026-01-01"),
        make_offer(first_payment_date="2026-01-31"),
        make_rules(),
    )
    assert not r.feasible


def test_fee_compliance():
    # fee fully collected, not before first payment, bank fee only on creditor dates
    client, offer, rules = load_case("cases/case1_feasible_even")
    r = evaluate_offer(client, offer, rules)
    assert r.feasible
    assert sum(row.program_fee_cents for row in r.schedule) == program_fee_cents(offer, rules)
    first_date = r.schedule[0].date
    for row in r.schedule:
        assert row.program_fee_cents == 0 or row.date >= first_date
        if row.creditor_payment_cents > 0:
            assert row.bank_fee_cents == rules.bank_fee_cents
        else:
            assert row.bank_fee_cents == 0


def test_fee_front_loaded():
    # fee collected early; once exhausted, later rows have zero fee
    client, offer, rules = load_case("cases/case4_tiers")
    r = evaluate_offer(client, offer, rules)
    assert r.feasible
    fees = [row.program_fee_cents for row in r.schedule]
    last_nonzero = max(i for i, f in enumerate(fees) if f > 0)
    assert all(fees[i] == 0 for i in range(last_nonzero + 1, len(fees)))


def test_part2_lump_sum():
    # lump sum reported is minimal: L makes it feasible, L-1 does not
    client, offer, rules = load_case("cases/case2_infeasible_minima")
    r = evaluate_offer(client, offer, rules)
    assert not r.feasible
    lump = r.additional_funds.lump_sum
    assert lump.within_guardrail is True
    assert lump.date <= client.last_draft_date

    def with_lump(amt):
        ledger = list(client.ledger) + [LedgerEntry(lump.date, amt, "credit")]
        c = Client(client.draft_amount_cents, client.draft_day, client.first_draft_date,
                   client.last_draft_date, client.as_of_date, client.current_balance_cents, ledger)
        return evaluate_offer(c, offer, rules).feasible

    assert with_lump(lump.amount_cents)
    if lump.amount_cents > 1:
        assert not with_lump(lump.amount_cents - 1)


def test_part2_monthly_increment():
    # monthly increment makes it feasible; num_drafts and guardrail correct
    client, offer, rules = load_case("cases/case2_infeasible_minima")
    r = evaluate_offer(client, offer, rules)
    inc = r.additional_funds.monthly_increment
    assert inc.within_guardrail is True
    expected_n = sum(1 for e in client.ledger if e.date > client.as_of_date and e.type == "credit")
    assert inc.num_drafts == expected_n

    new_ledger = [
        LedgerEntry(e.date, e.amount_cents + (inc.amount_cents if e.type == "credit" and e.date > client.as_of_date else 0), e.type)
        for e in client.ledger
    ]
    c2 = Client(client.draft_amount_cents + inc.amount_cents, client.draft_day, client.first_draft_date,
                client.last_draft_date, client.as_of_date, client.current_balance_cents, new_ledger)
    assert evaluate_offer(c2, offer, rules).feasible


def test_guardrails_rejected_when_too_large():
    # tiny drafts + large offer -> lump and increment exceed their guardrail limits
    r = evaluate_offer(
        make_client(draft_amount=1000, last_draft="2026-03-01"),
        make_offer(creditor_balance=200000),
        make_rules(max_k=3, min_payment=2500),
    )
    assert not r.feasible
    af = r.additional_funds
    if af.lump_sum.amount_cents > 65000:       # guardrail = round(0.65 * 100000)
        assert not af.lump_sum.within_guardrail
    if af.monthly_increment.amount_cents > 10000:  # guardrail = max(10000, 0.4*1000)
        assert not af.monthly_increment.within_guardrail


def test_micro_example_from_spec():
    # ASSIGNMENT.md §6: 3 dates, $100/month, offer_total=$250, fee=$50, min=$25
    # fee must be front-loaded (collected on first date)
    client = Client(10000, 1, date(2026, 1, 1), date(2026, 4, 1), date(2025, 12, 31), 0, [
        LedgerEntry(date(2026, 1, 1), 10000, "credit"),
        LedgerEntry(date(2026, 2, 1), 10000, "credit"),
        LedgerEntry(date(2026, 3, 1), 10000, "credit"),
        LedgerEntry(date(2026, 4, 1), 10000, "credit"),
    ])
    offer = Offer("MicroCo", 50000, 25000, 0.5, date(2026, 1, 31))
    rules = make_rules(max_k=3, min_payment=2500, max_segments=3, program_fee_pct=0.2)
    r = evaluate_offer(client, offer, rules)
    assert r.feasible
    assert sum(row.creditor_payment_cents for row in r.schedule) == 25000
    assert sum(row.program_fee_cents for row in r.schedule) == 5000
    assert r.schedule[0].program_fee_cents > 0
