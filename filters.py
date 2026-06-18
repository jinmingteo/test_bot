"""Stage 1: deterministic quantitative filter. No LLM, no tokens."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from data import StockData


@dataclass
class FilterResult:
    sd: StockData
    passed: bool
    margin_of_safety: float | None  # 1 - price/intrinsic; >0 means undervalued
    intrinsic_estimate: float | None  # the value used for MoS (best of Graham/DCF)
    intrinsic_method: str  # "graham" | "dcf" | "pb-proxy" | "none"
    intrinsic_graham: float | None
    intrinsic_dcf: float | None
    roe_mean: float | None
    roe_stdev: float | None
    reasons: list[str]


def graham_number(eps: float | None, bvps: float | None) -> float | None:
    if eps is None or bvps is None or eps <= 0 or bvps <= 0:
        return None
    return math.sqrt(22.5 * eps * bvps)


def owner_earnings_dcf(
    fcf_history: list[float],
    shares_outstanding: float | None,
    *,
    discount_rate: float = 0.10,
    terminal_growth: float = 0.025,
    explicit_years: int = 10,
    growth_cap: float = 0.08,
    growth_floor: float = 0.0,
) -> float | None:
    """Owner-earnings (FCF) 10y DCF, returns intrinsic value PER SHARE.
    Growth = clipped historical FCF CAGR over available history.
    Conservative defaults: Buffett-ish 10% discount, 2.5% terminal."""
    if not fcf_history or not shares_outstanding or shares_outstanding <= 0:
        return None

    # Base FCF: average of last 3 positive years (more stable than just TTM)
    recent = [f for f in fcf_history[:3] if f and f > 0]
    if not recent:
        return None
    base_fcf = sum(recent) / len(recent)

    # Historical CAGR over the available window (oldest -> newest)
    growth = 0.04  # fallback if too little history or negative
    pos = [f for f in fcf_history if f > 0]
    if len(pos) >= 3:
        oldest, newest = pos[-1], pos[0]
        years = len(pos) - 1
        if oldest > 0 and newest > 0 and years > 0:
            try:
                growth = (newest / oldest) ** (1 / years) - 1
            except (ValueError, ZeroDivisionError):
                growth = 0.04
    growth = max(growth_floor, min(growth_cap, growth))

    # Explicit projection
    pv = 0.0
    cf = base_fcf
    for yr in range(1, explicit_years + 1):
        cf = cf * (1 + growth)
        pv += cf / ((1 + discount_rate) ** yr)

    # Terminal (Gordon growth)
    if discount_rate <= terminal_growth:
        return None
    terminal_cf = cf * (1 + terminal_growth)
    terminal_value = terminal_cf / (discount_rate - terminal_growth)
    pv += terminal_value / ((1 + discount_rate) ** explicit_years)

    return pv / shares_outstanding


def roe_consistency(roe_history: list[float]) -> tuple[float | None, float | None, bool, bool]:
    """Returns (mean, stdev, declining_trend, too_volatile).
    declining_trend: avg of newest half is materially below avg of oldest half.
    too_volatile: coefficient of variation > 0.6."""
    clean = [r for r in roe_history if r is not None and not math.isnan(r)]
    if len(clean) < 2:
        return (None, None, False, False)
    mean = statistics.fmean(clean)
    stdev = statistics.pstdev(clean) if len(clean) >= 2 else 0.0
    too_volatile = bool(mean) and (abs(stdev / mean) > 0.6 if mean != 0 else False)

    declining = False
    if len(clean) >= 4:
        half = len(clean) // 2
        newest = statistics.fmean(clean[:half])  # most recent first
        oldest = statistics.fmean(clean[half:])
        # newest below oldest by > 30% relative → declining
        if oldest > 0 and newest < oldest * 0.7:
            declining = True
    return (mean, stdev, declining, too_volatile)


def evaluate(sd: StockData) -> FilterResult:
    reasons: list[str] = []

    if sd.fetch_error:
        return FilterResult(sd, False, None, None, "none", None, None, None, None,
                            [f"fetch error: {sd.fetch_error}"])

    if sd.price is None:
        return FilterResult(sd, False, None, None, "none", None, None, None, None, ["no price"])

    # Liquidity (market-specific floor in the stock's reporting currency)
    cap_floor = {"SGX": 500e6, "HK": 5e9, "US": 1e9}.get(sd.market, 500e6)
    if sd.market_cap is None or sd.market_cap < cap_floor:
        reasons.append(
            f"market cap below {sd.currency} {cap_floor/1e6:.0f}M ({sd.market_cap})"
        )

    # Branching thresholds
    if sd.is_reit:
        pb_max = 1.3
        de_max = 0.5 / 0.5  # gearing proxy; allow higher leverage for REITs
        roe_min = 5.0
        roic_min = None
    elif sd.is_bank:
        pb_max = 1.5
        de_max = None  # not meaningful for banks
        roe_min = 8.0
        roic_min = None  # ROIC isn't meaningful for banks (no operating capital structure)
    else:
        pb_max = 2.5
        de_max = 1.0
        roe_min = 10.0
        roic_min = 10.0

    if sd.pe is None or sd.pe <= 0:
        reasons.append("no positive P/E")
    elif sd.pe > 25:
        reasons.append(f"P/E too high ({sd.pe:.1f})")

    if sd.pb is None:
        reasons.append("no P/B")
    elif sd.pb > pb_max:
        reasons.append(f"P/B {sd.pb:.2f} > {pb_max}")

    if sd.roe is None:
        reasons.append("no ROE")
    elif sd.roe < roe_min:
        reasons.append(f"ROE {sd.roe:.1f}% < {roe_min}%")

    if de_max is not None and sd.debt_to_equity is not None and sd.debt_to_equity > de_max:
        reasons.append(f"D/E {sd.debt_to_equity:.2f} > {de_max}")

    if not (sd.is_bank or sd.is_reit) and sd.fcf_positive_years < 4:
        reasons.append(f"only {sd.fcf_positive_years}/5 positive FCF years (need 4)")

    # ROIC — quality screen for non-financials (the metric Buffett actually targets)
    if roic_min is not None:
        if sd.roic is None:
            # Don't auto-fail (statements may be missing for some yfinance tickers),
            # but flag in completeness. Only enforce when present.
            pass
        elif sd.roic < roic_min:
            reasons.append(f"ROIC {sd.roic:.1f}% < {roic_min}%")

    # ROE consistency — reject one-off-good-year cyclicals
    roe_mean, roe_stdev, declining, too_volatile = roe_consistency(sd.roe_history)
    if not sd.is_reit:  # REIT earnings are noisy by design (revaluations)
        if declining:
            reasons.append(
                f"ROE trend declining ({sd.roe_history[0]:.1f}% latest vs older history)"
            )
        if too_volatile and roe_mean is not None:
            reasons.append(
                f"ROE too volatile (mean {roe_mean:.1f}%, stdev {roe_stdev:.1f}%)"
            )

    # ===== Intrinsic value =====
    intrinsic_graham = None
    intrinsic_dcf = None
    intrinsic = None
    method = "none"
    mos = None

    if sd.is_bank or sd.is_reit:
        # P/B proxy — Graham/DCF not meaningful for financials
        if sd.pb is not None:
            mos = 1 - (sd.pb / pb_max)
            method = "pb-proxy"
    else:
        intrinsic_graham = graham_number(sd.eps, sd.bvps)
        intrinsic_dcf = owner_earnings_dcf(sd.fcf_history, sd.shares_outstanding)

        # Pick best (highest) intrinsic — but DCF is the preferred number when available
        candidates = [(intrinsic_dcf, "dcf"), (intrinsic_graham, "graham")]
        candidates = [(v, m) for v, m in candidates if v is not None and v > 0]
        if candidates:
            intrinsic, method = max(candidates, key=lambda x: x[0])
            if sd.price:
                mos = 1 - (sd.price / intrinsic)
                if mos < 0.15:
                    reasons.append(
                        f"margin of safety {mos*100:.1f}% < 15% "
                        f"({method} intrinsic {intrinsic:.2f})"
                    )
        else:
            reasons.append("no intrinsic value (insufficient data for Graham or DCF)")

    passed = len(reasons) == 0
    return FilterResult(
        sd=sd,
        passed=passed,
        margin_of_safety=mos,
        intrinsic_estimate=intrinsic,
        intrinsic_method=method,
        intrinsic_graham=intrinsic_graham,
        intrinsic_dcf=intrinsic_dcf,
        roe_mean=roe_mean,
        roe_stdev=roe_stdev,
        reasons=reasons,
    )


def stage1(stocks: list[StockData]) -> list[FilterResult]:
    results = [evaluate(sd) for sd in stocks]
    # rank passers by margin of safety desc
    results.sort(
        key=lambda r: (r.passed, r.margin_of_safety if r.margin_of_safety is not None else -1),
        reverse=True,
    )
    return results
