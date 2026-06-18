"""Telegram bot: runs Stage 1 token-free filter across markets and sends per-stock messages.

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
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request

from dotenv import load_dotenv

import data
import filters

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


def pros_and_cons(fr: filters.FilterResult) -> tuple[list[str], list[str]]:
    """Derive pros (criteria comfortably beaten) and cons (criteria closest to limit)
    from the Stage 1 metrics. Token-free."""
    sd = fr.sd
    pros: list[str] = []
    cons: list[str] = []

    # Thresholds mirror filters.evaluate()
    if sd.is_reit:
        pe_max, pb_max, roe_min = 25, 1.3, 5.0
    elif sd.is_bank:
        pe_max, pb_max, roe_min = 25, 1.5, 8.0
    else:
        pe_max, pb_max, roe_min = 25, 2.5, 10.0

    # Margin of safety — always a pro for passers
    if fr.margin_of_safety is not None:
        tag = f" ({fr.intrinsic_method})" if fr.intrinsic_method != "none" else ""
        pros.append(f"Margin of safety {fr.margin_of_safety*100:+.1f}%{tag}")

    # Score each criterion as (slack ratio, label, side)
    # Lower slack = closer to limit = more "con"-ish.
    scored: list[tuple[float, str]] = []

    if sd.pe is not None and sd.pe > 0:
        slack = (pe_max - sd.pe) / pe_max
        scored.append((slack, f"P/E {sd.pe:.1f} (limit {pe_max:.0f})"))
    if sd.pb is not None:
        slack = (pb_max - sd.pb) / pb_max
        scored.append((slack, f"P/B {sd.pb:.2f} (limit {pb_max:.1f})"))
    if sd.roe is not None:
        slack = (sd.roe - roe_min) / max(roe_min, 1.0)
        scored.append((slack, f"ROE {sd.roe:.1f}% (min {roe_min:.0f}%)"))
    if sd.roic is not None and not (sd.is_reit or sd.is_bank):
        slack = (sd.roic - 10.0) / 10.0
        scored.append((slack, f"ROIC {sd.roic:.1f}% (min 10%)"))

    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        # Top half = pros, weakest one = con
        strong = scored[:-1] if len(scored) > 1 else scored
        for _, label in strong:
            pros.append(label)
        weakest_slack, weakest_label = scored[-1]
        if len(scored) > 1 and weakest_slack < 0.5:
            cons.append(f"Tight on: {weakest_label}")

    # ROE consistency notes
    if fr.roe_mean is not None and fr.roe_stdev is not None:
        cv = fr.roe_stdev / fr.roe_mean if fr.roe_mean else 0
        if cv > 0.35:
            cons.append(f"ROE volatile (μ={fr.roe_mean:.1f}%, σ={fr.roe_stdev:.1f}%)")
        else:
            pros.append(f"ROE consistent (μ={fr.roe_mean:.1f}%, σ={fr.roe_stdev:.1f}%)")

    # Data completeness
    if sd.data_completeness < 0.7:
        cons.append(f"Data coverage {sd.data_completeness*100:.0f}% — verify manually")

    if not cons:
        cons.append("None flagged by quantitative filter")

    return pros, cons


def format_stock(fr: filters.FilterResult) -> str:
    sd = fr.sd
    pros, cons = pros_and_cons(fr)
    lines = [
        f"<b>{sd.ticker}</b> — {sd.name or 'n/a'}",
    ]
    tags = []
    if sd.is_reit:
        tags.append("REIT")
    if sd.is_bank:
        tags.append("Bank")
    if sd.sector:
        tags.append(sd.sector)
    if tags:
        lines.append(f"<i>{' · '.join(tags)}</i>")
    if sd.price is not None:
        lines.append(f"Price: {sd.currency} {sd.price:.2f}")
    lines.append("")
    lines.append("<b>Pros</b>")
    for p in pros:
        lines.append(f"  ✅ {p}")
    lines.append("")
    lines.append("<b>Cons</b>")
    for c in cons:
        lines.append(f"  ⚠️ {c}")
    return "\n".join(lines)


def run_market(token: str, chat_id: str, market: str, top: int) -> int:
    universe_path = ROOT / f"universe_{market}.txt"
    tickers = data.load_universe(str(universe_path))
    stocks = data.fetch_universe(tickers)
    results = filters.stage1(stocks)
    passers = [r for r in results if r.passed][:top]

    header = (
        f"📊 <b>{market.upper()} Buffett Finder</b> — {dt.date.today().isoformat()}\n"
        f"Universe: {len(tickers)} · Shortlist: {len(passers)}"
    )
    send_message(token, chat_id, header)
    time.sleep(0.5)

    if not passers:
        send_message(token, chat_id, f"No {market.upper()} passers today.")
        return 0

    for fr in passers:
        send_message(token, chat_id, format_stock(fr))
        time.sleep(0.5)  # respect Telegram rate limits
    return len(passers)


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
            err = f"❌ {m.upper()} run failed: {e}"
            print(err, file=sys.stderr)
            try:
                send_message(token, chat_id, err)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
