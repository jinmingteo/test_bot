"""Persist per-market run snapshots so daily output can talk about what changed."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
from dataclasses import dataclass, field

from filters import FilterResult, group_key

ROOT = pathlib.Path(__file__).parent
STATE_DIR = ROOT / "state"


@dataclass
class TickerDelta:
    ticker: str
    status: str  # new | moved | stale | returned
    streak: int
    first_seen: str
    mos: float | None
    mos_prev: float | None
    price: float | None
    price_prev: float | None
    composite: float | None
    last_deep_dive: str | None

    @property
    def mos_delta(self) -> float | None:
        if self.mos is None or self.mos_prev is None:
            return None
        return self.mos - self.mos_prev

    @property
    def price_chg(self) -> float | None:
        if self.price is None or self.price_prev is None or self.price_prev == 0:
            return None
        return self.price / self.price_prev - 1.0


@dataclass
class MarketDelta:
    first_snapshot: bool
    new: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    returned: list[str] = field(default_factory=list)
    by_ticker: dict[str, TickerDelta] = field(default_factory=dict)
    dropped_detail: dict[str, dict] = field(default_factory=dict)


def _path(market: str) -> pathlib.Path:
    return STATE_DIR / f"{market.lower()}.json"


def load(market: str) -> dict:
    p = _path(market)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(
    market: str,
    passers: list[FilterResult],
    today: dt.date | None = None,
    deep_dived: list[str] | None = None,
) -> None:
    today = today or dt.date.today()
    today_s = today.isoformat()
    prev = load(market)
    prev_tickers: dict = prev.get("tickers") or {}
    deep_dived = set(deep_dived or [])

    tickers: dict[str, dict] = {}
    passer_set = {fr.sd.ticker for fr in passers}
    for fr in passers:
        old = prev_tickers.get(fr.sd.ticker) or {}
        last_seen = old.get("last_seen")
        first_seen = old.get("first_seen") or today_s
        streak = int(old.get("streak") or 0)
        if last_seen == today_s:
            pass  # same-day rerun
        elif last_seen:
            try:
                gap = (today - dt.date.fromisoformat(last_seen)).days
            except ValueError:
                gap = 99
            streak = streak + 1 if gap <= 4 else 1
        else:
            streak = 1
        last_dd = today_s if fr.sd.ticker in deep_dived else old.get("last_deep_dive")
        tickers[fr.sd.ticker] = {
            "name": fr.sd.name,
            "price": fr.sd.price,
            "mos": fr.margin_of_safety,
            "composite": fr.composite,
            "sector": group_key(fr.sd),
            "first_seen": first_seen,
            "last_seen": today_s,
            "streak": streak,
            "last_deep_dive": last_dd,
        }

    # Keep recently dropped names around so the next run can say what fell off.
    cutoff = (today - dt.timedelta(days=30)).isoformat()
    for tk, rec in prev_tickers.items():
        if tk in passer_set:
            continue
        rec = dict(rec)
        rec["streak"] = 0
        if (rec.get("last_seen") or "") >= cutoff:
            tickers[tk] = rec

    STATE_DIR.mkdir(exist_ok=True)
    payload = {"updated": today_s, "tickers": tickers}
    _path(market).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def diff(market: str, passers: list[FilterResult], today: dt.date | None = None) -> MarketDelta:
    today = today or dt.date.today()
    prev = load(market)
    prev_tickers: dict = prev.get("tickers") or {}
    first = not prev_tickers
    out = MarketDelta(first_snapshot=first)
    if first:
        for fr in passers:
            out.by_ticker[fr.sd.ticker] = TickerDelta(
                ticker=fr.sd.ticker,
                status="stale",
                streak=1,
                first_seen=today.isoformat(),
                mos=fr.margin_of_safety,
                mos_prev=None,
                price=fr.sd.price,
                price_prev=None,
                composite=fr.composite,
                last_deep_dive=None,
            )
            out.stale.append(fr.sd.ticker)
        return out

    today_set = {fr.sd.ticker for fr in passers}
    prev_active = {
        tk for tk, rec in prev_tickers.items()
        if rec.get("streak", 0) > 0 and rec.get("last_seen")
    }

    for fr in passers:
        old = prev_tickers.get(fr.sd.ticker) or {}
        last_seen = old.get("last_seen")
        first_seen = old.get("first_seen") or today.isoformat()
        streak = int(old.get("streak") or 0)
        status = "new"
        if not old:
            status = "new"
            streak = 1
        else:
            try:
                gap = (today - dt.date.fromisoformat(last_seen)).days if last_seen else 99
            except ValueError:
                gap = 99
            if last_seen == today.isoformat():
                streak = max(streak, 1)
            elif gap <= 4:
                streak = streak + 1
            else:
                streak = 1
                status = "returned"

            mos_prev = old.get("mos")
            price_prev = old.get("price")
            moved = False
            if fr.margin_of_safety is not None and isinstance(mos_prev, (int, float)):
                if abs(fr.margin_of_safety - mos_prev) >= 0.05:
                    moved = True
            if fr.sd.price and isinstance(price_prev, (int, float)) and price_prev:
                if abs(fr.sd.price / price_prev - 1.0) >= 0.08:
                    moved = True
            if status != "returned":
                if not last_seen:
                    status = "new"
                elif moved:
                    status = "moved"
                else:
                    status = "stale"

        td = TickerDelta(
            ticker=fr.sd.ticker,
            status=status,
            streak=streak,
            first_seen=first_seen,
            mos=fr.margin_of_safety,
            mos_prev=old.get("mos") if isinstance(old.get("mos"), (int, float)) else None,
            price=fr.sd.price,
            price_prev=old.get("price") if isinstance(old.get("price"), (int, float)) else None,
            composite=fr.composite,
            last_deep_dive=old.get("last_deep_dive"),
        )
        out.by_ticker[fr.sd.ticker] = td
        getattr(out, status).append(fr.sd.ticker)

    for tk in prev_active:
        if tk not in today_set:
            out.dropped.append(tk)
            out.dropped_detail[tk] = prev_tickers.get(tk) or {}
    return out


def pick_deep_dives(
    passers: list[FilterResult],
    delta: MarketDelta,
    max_n: int,
    today: dt.date | None = None,
    refresh_after_days: int = 14,
) -> list[FilterResult]:
    """Deep-dive new / moved / returned names; refresh stale ones after 2 weeks."""
    today = today or dt.date.today()
    if delta.first_snapshot:
        return passers[:max_n]

    def days_since(iso: str | None) -> int:
        if not iso:
            return 10_000
        try:
            return (today - dt.date.fromisoformat(iso)).days
        except ValueError:
            return 10_000

    chosen: list[FilterResult] = []
    for fr in passers:
        d = delta.by_ticker.get(fr.sd.ticker)
        if d is None:
            continue
        if d.status in ("new", "moved", "returned"):
            chosen.append(fr)
        elif days_since(d.last_deep_dive) >= refresh_after_days:
            chosen.append(fr)
        if len(chosen) >= max_n:
            break
    return chosen[:max_n]


def extra_bundle_lines(fr: FilterResult, delta: MarketDelta, lens_title: str) -> str:
    d = delta.by_ticker.get(fr.sd.ticker)
    mos = f"{fr.margin_of_safety*100:+.1f}%" if fr.margin_of_safety is not None else "n/a"
    iv = f"{fr.intrinsic_estimate:.2f}" if fr.intrinsic_estimate is not None else "n/a"
    bits = [
        f"Weekly analysis lens: {lens_title}",
        (
            f"Stage 1 intrinsic: {iv} [{fr.intrinsic_method}]  MoS {mos}  "
            f"composite {fr.composite:.0f}" if fr.composite is not None else
            f"Stage 1 intrinsic: {iv} [{fr.intrinsic_method}]  MoS {mos}"
        ),
        (
            f"Score mix: cheap {fr.cheapness:.2f} · quality {fr.quality:.2f} · "
            f"setup {fr.setup:.2f}" if fr.cheapness is not None else ""
        ),
    ]
    bits = [b for b in bits if b]
    if d is None or delta.first_snapshot:
        bits.append("Run context: baseline snapshot (no prior run to compare).")
        return "\n".join(bits)
    mos_now = f"{d.mos*100:+.1f}%" if d.mos is not None else "n/a"
    mos_prev = f"{d.mos_prev*100:+.1f}%" if d.mos_prev is not None else "n/a"
    chg = ""
    if d.mos_delta is not None:
        chg = f" (MoS {mos_prev} → {mos_now}, {d.mos_delta*100:+.1f}pp)"
    px = ""
    if d.price_chg is not None:
        px = f", price {d.price_chg*100:+.1f}%"
    bits.append(
        f"Run context: {d.status.upper()} · day {d.streak} on the shortlist{chg}{px}. "
        f"First seen {d.first_seen}."
    )
    if d.status == "stale":
        bits.append(
            "This name has been on the shortlist recently. Do NOT rewrite a generic moat essay. "
            "Focus on what changed, the bear case, and whether the thesis is breaking."
        )
    return "\n".join(bits)
