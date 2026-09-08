"""Unit tests for ranking, conservative IV, deltas. No network."""
from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from data import StockData
import filters
import lenses
import state


def _co(ticker="AAA", **kw) -> StockData:
    defaults = dict(
        ticker=ticker,
        name=ticker,
        sector="Industrials",
        industry="Conglomerates",
        currency="SGD",
        market="SGX",
        price=10.0,
        market_cap=2e9,
        pe=12.0,
        pb=1.4,
        roe=18.0,
        debt_to_equity=0.3,
        fcf_positive_years=5,
        fcf_history=[120e6, 110e6, 100e6, 90e6, 80e6],
        eps=1.0,
        bvps=7.0,
        shares_outstanding=100e6,
        roe_history=[18.0, 17.0, 16.5, 16.0],
        roic=14.0,
        operating_margin=18.0,
        interest_coverage=12.0,
        pct_from_high=-0.20,
        return_1y=-0.10,
        week_52_high=12.5,
        week_52_low=8.0,
    )
    defaults.update(kw)
    return StockData(**defaults)


class ConservativeIV(unittest.TestCase):
    def test_picks_lower_of_graham_and_dcf(self):
        # Graham = sqrt(22.5 * 1 * 7) ≈ 12.55. DCF on rising FCF will be much higher.
        sd = _co()
        fr = filters.evaluate(sd)
        self.assertIsNotNone(fr.intrinsic_graham)
        self.assertIsNotNone(fr.intrinsic_dcf)
        self.assertLess(fr.intrinsic_estimate, max(fr.intrinsic_graham, fr.intrinsic_dcf) + 1e-9)
        self.assertAlmostEqual(
            fr.intrinsic_estimate,
            min(fr.intrinsic_graham, fr.intrinsic_dcf),
            places=4,
        )
        self.assertIn("conservative", fr.intrinsic_method)

    def test_bank_justified_pb(self):
        sd = _co(ticker="D05.SI", sector="Financial Services", is_bank=True, pb=1.2, roe=18.0, pe=10.0)
        fr = filters.evaluate(sd)
        self.assertEqual(fr.intrinsic_method, "justified-pb")
        # justified PB = 1.8, mos = 1 - 1.2/1.8 = 0.333
        self.assertAlmostEqual(fr.margin_of_safety, 1 - 1.2 / 1.8, places=4)

    def test_op_margin_12_no_longer_fails(self):
        sd = _co(operating_margin=12.0)
        fr = filters.evaluate(sd)
        self.assertTrue(fr.passed, fr.reasons)

    def test_op_margin_5_still_fails(self):
        sd = _co(operating_margin=5.0)
        fr = filters.evaluate(sd)
        self.assertFalse(fr.passed)
        self.assertTrue(any("operating margin" in r for r in fr.reasons))


class Ranking(unittest.TestCase):
    def test_sector_cap_prevents_all_reits(self):
        reits = [
            filters.evaluate(_co(ticker=f"R{i}.SI", is_reit=True, sector="Real Estate",
                                 pb=1.0, roe=8.0, pe=12.0, dividend_yield=7.0))
            for i in range(5)
        ]
        others = [
            filters.evaluate(_co(ticker="X1.SI", sector="Consumer Defensive")),
            filters.evaluate(_co(ticker="X2.SI", sector="Industrials", pb=1.1)),
        ]
        picked = filters.pick_shortlist(reits + others, max_n=5, max_per_group=2)
        n_reit = sum(1 for r in picked if r.sd.is_reit)
        self.assertLessEqual(n_reit, 2)
        self.assertGreaterEqual(len(picked), 3)

    def test_composite_prefers_quality_drawdown_over_pure_mos(self):
        cheap_trap = filters.evaluate(_co(
            ticker="TRAP", price=5.0, pe=8.0, pb=0.6, roe=11.0, roic=10.5,
            operating_margin=9.0, pct_from_high=-0.02, return_1y=0.05,
            eps=0.6, bvps=8.0,
        ))
        quality_dip = filters.evaluate(_co(
            ticker="QUAL", price=8.0, pe=14.0, pb=1.6, roe=22.0, roic=18.0,
            operating_margin=25.0, pct_from_high=-0.30, return_1y=-0.18,
        ))
        self.assertTrue(cheap_trap.passed, cheap_trap.reasons)
        self.assertTrue(quality_dip.passed, quality_dip.reasons)
        self.assertGreater(quality_dip.composite, cheap_trap.composite)

    def test_fcf_cagr_uses_endpoints_not_positive_only(self):
        # Oldest 100 → newest 80 over 4 years is negative even if a middle year is a loss.
        hist = [80.0, 90.0, -10.0, 95.0, 100.0]
        cagr = filters.fcf_cagr(hist)
        self.assertIsNotNone(cagr)
        self.assertLess(cagr, 0)


class Lenses(unittest.TestCase):
    def test_rotates_by_iso_week(self):
        a = lenses.current_lens(dt.date(2026, 1, 5))
        b = lenses.current_lens(dt.date(2026, 1, 12))
        self.assertNotEqual(a["id"], b["id"])

    def test_override(self):
        ln = lenses.current_lens(lens_id="inversion")
        self.assertEqual(ln["id"], "inversion")


class Delta(unittest.TestCase):
    def test_first_snapshot_then_stale_then_drop(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(state, "STATE_DIR", Path(tmp)):
                a = filters.evaluate(_co(ticker="A.SI"))
                b = filters.evaluate(_co(ticker="B.SI", sector="Healthcare"))
                d0 = state.diff("SGX", [a, b], today=dt.date(2026, 9, 1))
                self.assertTrue(d0.first_snapshot)
                state.save("SGX", [a, b], today=dt.date(2026, 9, 1))

                d1 = state.diff("SGX", [a, b], today=dt.date(2026, 9, 2))
                self.assertFalse(d1.first_snapshot)
                self.assertEqual(set(d1.stale), {"A.SI", "B.SI"})
                self.assertEqual(d1.new, [])

                d2 = state.diff("SGX", [a], today=dt.date(2026, 9, 3))
                self.assertEqual(d2.dropped, ["B.SI"])
                self.assertIn("A.SI", d2.stale)

    def test_moved_on_mos_jump(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(state, "STATE_DIR", Path(tmp)):
                a = filters.evaluate(_co(ticker="A.SI", price=10.0))
                state.save("SGX", [a], today=dt.date(2026, 9, 1))
                cheaper = filters.evaluate(_co(ticker="A.SI", price=7.0))
                d = state.diff("SGX", [cheaper], today=dt.date(2026, 9, 2))
                self.assertEqual(d.by_ticker["A.SI"].status, "moved")

    def test_deep_dive_skips_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(state, "STATE_DIR", Path(tmp)):
                a = filters.evaluate(_co(ticker="A.SI"))
                state.save("SGX", [a], today=dt.date(2026, 9, 1), deep_dived=["A.SI"])
                d = state.diff("SGX", [a], today=dt.date(2026, 9, 2))
                picked = state.pick_deep_dives([a], d, max_n=5, today=dt.date(2026, 9, 2))
                self.assertEqual(picked, [])


if __name__ == "__main__":
    unittest.main()
