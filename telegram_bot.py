"""Telegram bot: Stage 1 token-free filter, delta-first messages.

Env vars required:
  TELEGRAM_BOT_TOKEN   bot token from @BotFather
  TELEGRAM_CHAT_ID     your chat id (talk to @userinfobot)

Usage:
  python telegram_bot.py                 # all markets, top 5 each
  python telegram_bot.py --top 3 --markets sgx us
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request

from dotenv import load_dotenv

import data
import filters
import lenses
import state

ROOT = pathlib.Path(__file__).parent
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_message(token: str, chat_id: str, text: str) -> None:
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(
        TELEGRAM_API.format(token=token),
        data=payload,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Telegram API {resp.status}: {resp.read()[:200]}")


def _esc(s: str) -> str:
    return html.escape(s, quote=False)


def why_today(fr: filters.FilterResult, d: state.TickerDelta | None) -> str:
    """One sentence that is not a restatement of the filter thresholds."""
    bits: list[str] = []
    sd = fr.sd
    if d and d.status == "new":
        bits.append("first time through the screen")
    elif d and d.status == "returned":
        bits.append("back on the shortlist after dropping off")
    elif d and d.status == "moved":
        if d.mos_delta is not None:
            bits.append(f"MoS moved {d.mos_delta*100:+.0f}pp")
        if d.price_chg is not None:
            bits.append(f"price {d.price_chg*100:+.0f}%")
    if sd.pct_from_high is not None and sd.pct_from_high < -0.15:
        bits.append(f"{abs(sd.pct_from_high)*100:.0f}% below 52w high")
    elif sd.pct_from_high is not None and sd.pct_from_high > -0.05:
        bits.append("sitting near 52w high — cheap vs model, not vs itself")
    if sd.news:
        title = (sd.news[0].get("title") or "").strip()
        if title:
            bits.append(f"news: {title[:90]}")
    if not bits:
        bits.append("passes the screen; nothing new versus last run")
    return "; ".join(bits[:3])


def format_full(fr: filters.FilterResult, d: state.TickerDelta | None) -> str:
    sd = fr.sd
    if d is None:
        tag = ""
    elif d.mos_prev is None and d.status == "stale":
        tag = f"day {d.streak}"
    else:
        tag = f"{d.status.upper()} · day {d.streak}"
    score = f"{fr.composite:.0f}" if fr.composite is not None else "n/a"
    mos = f"{fr.margin_of_safety*100:+.1f}%" if fr.margin_of_safety is not None else "n/a"
    method = f" ({fr.intrinsic_method})" if fr.intrinsic_method not in ("none", "") else ""
    from_high = ""
    if sd.pct_from_high is not None:
        from_high = f"{sd.pct_from_high*100:+.0f}% vs 52w high"
    ret = ""
    if sd.return_1y is not None:
        ret = f" · 1y {sd.return_1y*100:+.0f}%"

    tags = []
    if sd.is_reit:
        tags.append("REIT")
    if sd.is_bank:
        tags.append("Bank")
    if sd.sector:
        tags.append(sd.sector)

    meta = [tag] if tag else []
    meta.extend(tags)
    lines = [
        f"<b>{_esc(sd.ticker)}</b> — {_esc(sd.name or 'n/a')}",
    ]
    if meta:
        lines.append(f"<i>{_esc(' · '.join(meta))}</i>")
    if sd.price is not None:
        lines.append(f"Price: {sd.currency} {sd.price:.2f}" + (f" · {from_high}" if from_high else "") + ret)
    lines.append("")
    lines.append(f"Score <b>{score}</b>  ·  MoS {mos}{method}")
    q = fr.quality if fr.quality is not None else 0
    c = fr.cheapness if fr.cheapness is not None else 0
    s = fr.setup if fr.setup is not None else 0
    lines.append(f"cheap {c:.2f} · quality {q:.2f} · setup {s:.2f}")
    lines.append("")
    lines.append("<b>Why today</b>")
    lines.append(_esc(why_today(fr, d)))
    return "\n".join(lines)


def format_stale(fr: filters.FilterResult, d: state.TickerDelta | None) -> str:
    sd = fr.sd
    score = f"{fr.composite:.0f}" if fr.composite is not None else "n/a"
    mos = f"{fr.margin_of_safety*100:+.1f}%" if fr.margin_of_safety is not None else "n/a"
    prev = ""
    if d and d.mos_prev is not None and d.mos is not None:
        prev = f" ({d.mos_prev*100:+.0f}%→{d.mos*100:+.0f}%)"
    streak = d.streak if d else "?"
    return (
        f"STALE  <b>{_esc(sd.ticker)}</b>  day {streak}  "
        f"score {score}  MoS {mos}{prev}"
    )


def format_wildcard(fr: filters.FilterResult) -> str:
    sd = fr.sd
    score = f"{fr.composite:.0f}" if fr.composite is not None else "n/a"
    from_high = ""
    if sd.pct_from_high is not None:
        from_high = f" · {sd.pct_from_high*100:+.0f}% vs 52w high"
    return (
        f"<b>WILDCARD</b> {_esc(sd.ticker)} — {_esc(sd.name or 'n/a')}\n"
        f"score {score}{from_high}\n"
        f"failed: {_esc('; '.join(fr.reasons))}"
    )


def format_header(
    market: str,
    n_universe: int,
    n_short: int,
    delta: state.MarketDelta,
    lens: dict,
) -> str:
    today = dt.date.today().isoformat()
    lines = [
        f"<b>{market.upper()} Buffett Finder</b> — {today}",
        f"<i>Lens: {_esc(lens['title'])}</i>",
        f"Universe {n_universe} · Shortlist {n_short}",
    ]
    if delta.first_snapshot:
        lines.append("Baseline snapshot — later runs will lead with what changed.")
    else:
        lines.append(
            f"New {len(delta.new)} · Returned {len(delta.returned)} · "
            f"Moved {len(delta.moved)} · Stale {len(delta.stale)} · "
            f"Dropped {len(delta.dropped)}"
        )
        if delta.dropped:
            lines.append("Dropped: " + ", ".join(_esc(t) for t in delta.dropped))
    return "\n".join(lines)


def run_market(token: str, chat_id: str, market: str, top: int) -> int:
    universe_path = ROOT / f"universe_{market}.txt"
    tickers = data.load_universe(str(universe_path))
    stocks = data.fetch_universe(tickers)
    results = filters.stage1(stocks)
    shortlist = filters.pick_shortlist(results, top)
    wildcard = filters.pick_wildcard(results, shortlist)
    delta = state.diff(market.upper(), shortlist)
    lens = lenses.current_lens()

    send_message(token, chat_id, format_header(market, len(tickers), len(shortlist), delta, lens))
    time.sleep(0.5)

    if not shortlist:
        send_message(token, chat_id, f"No {market.upper()} passers today.")
        state.save(market.upper(), shortlist)
        return 0

    # Full cards: new / moved / returned. Stale names collapse to one-liners
    # unless this is the baseline snapshot (no prior memory).
    full, stale = [], []
    for fr in shortlist:
        d = delta.by_ticker.get(fr.sd.ticker)
        if delta.first_snapshot or (d and d.status in ("new", "moved", "returned")):
            full.append(fr)
        else:
            stale.append(fr)

    for fr in full:
        send_message(token, chat_id, format_full(fr, delta.by_ticker.get(fr.sd.ticker)))
        time.sleep(0.5)

    if stale:
        body = "\n".join(format_stale(fr, delta.by_ticker.get(fr.sd.ticker)) for fr in stale)
        send_message(token, chat_id, "<b>Still here</b> (unchanged thesis)\n" + body)
        time.sleep(0.5)

    if wildcard:
        send_message(token, chat_id, format_wildcard(wildcard))
        time.sleep(0.5)

    state.save(market.upper(), shortlist)
    return len(shortlist)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", nargs="+", default=["sgx", "us", "hk"],
                    choices=["sgx", "us", "hk"])
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args()

    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("ERROR: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID", file=sys.stderr)
        return 1

    for m in args.markets:
        try:
            n = run_market(token, chat_id, m, args.top)
            print(f"[{m}] sent {n} passers", file=sys.stderr)
        except Exception as e:
            err = f"{m.upper()} run failed: {e}"
            print(err, file=sys.stderr)
            try:
                send_message(token, chat_id, err)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
