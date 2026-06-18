"""Buffett-style SGX undervalued stock finder. Batch pipeline."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys

from dotenv import load_dotenv

import data
import filters
import prompts


def load_claude_settings_env() -> None:
    """Load env vars from Claude Code's settings.json so Foundry credentials are available."""
    for path in (
        pathlib.Path.home() / ".claude" / "settings.json",
        pathlib.Path.cwd() / ".claude" / "settings.json",
    ):
        if not path.exists():
            continue
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for k, v in (cfg.get("env") or {}).items():
            os.environ.setdefault(k, str(v))


HAIKU_MODEL = os.getenv("BUFFETT_HAIKU_MODEL", "claude-haiku-4-5")
OPUS_MODEL = os.getenv("BUFFETT_OPUS_MODEL", "claude-opus-4-7")

# Approx pricing (USD per 1M tokens) for cost estimation
PRICING = {
    HAIKU_MODEL: {"in": 1.00, "cache_read": 0.10, "cache_write": 1.25, "out": 5.00},
    OPUS_MODEL: {"in": 15.00, "cache_read": 1.50, "cache_write": 18.75, "out": 75.00},
}

ROOT = pathlib.Path(__file__).parent
LOG_PATH = ROOT / "run_log.jsonl"


def cached_system_block(market: str) -> list[dict]:
    return [
        {
            "type": "text",
            "text": prompts.system_prompt(market),
            "cache_control": {"type": "ephemeral"},
        }
    ]


def log_usage(stage: str, ticker: str, model: str, usage) -> dict:
    rec = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "stage": stage,
        "ticker": ticker,
        "model": model,
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def estimate_cost(records: list[dict]) -> float:
    total = 0.0
    for r in records:
        p = PRICING.get(r["model"])
        if not p:
            continue
        total += r["input_tokens"] / 1e6 * p["in"]
        total += r["cache_read_input_tokens"] / 1e6 * p["cache_read"]
        total += r["cache_creation_input_tokens"] / 1e6 * p["cache_write"]
        total += r["output_tokens"] / 1e6 * p["out"]
    return total


def extract_text(message) -> str:
    parts = []
    for block in message.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def haiku_triage(client, ticker: str, bundle: str, market: str) -> tuple[str, dict]:
    msg = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=400,
        system=cached_system_block(market),
        messages=[
            {
                "role": "user",
                "content": f"Data bundle:\n\n{bundle}\n\n{prompts.HAIKU_TRIAGE_TASK}",
            }
        ],
    )
    rec = log_usage("triage", ticker, HAIKU_MODEL, msg.usage)
    return extract_text(msg), rec


def opus_deep_dive(client, ticker: str, bundle: str, market: str) -> tuple[str, dict]:
    msg = client.messages.create(
        model=OPUS_MODEL,
        max_tokens=2000,
        system=cached_system_block(market),
        messages=[
            {
                "role": "user",
                "content": f"Data bundle:\n\n{bundle}\n\n{prompts.OPUS_DEEP_DIVE_TASK}",
            }
        ],
    )
    rec = log_usage("deep_dive", ticker, OPUS_MODEL, msg.usage)
    return extract_text(msg), rec


def write_report(
    out_path: pathlib.Path,
    market: str,
    universe_size: int,
    stage1_results: list[filters.FilterResult],
    triage: list[tuple[filters.FilterResult, str]],
    finalists: list[tuple[filters.FilterResult, str]],
    records: list[dict],
) -> None:
    passers = [r for r in stage1_results if r.passed]
    dropped = [r for r in stage1_results if not r.passed]
    total_in = sum(r["input_tokens"] for r in records)
    total_out = sum(r["output_tokens"] for r in records)
    total_cache_read = sum(r["cache_read_input_tokens"] for r in records)
    cost = estimate_cost(records)

    lines = [
        f"# {market} Buffett Finder Report — {dt.date.today().isoformat()}",
        "",
        "## Pipeline summary",
        f"- Universe size: {universe_size}",
        f"- Stage 1 passers (shortlist): {len(passers)}",
        f"- Stage 2 triage promotions: {len(finalists)}",
        f"- Stage 3 deep dives: {len(finalists)}",
        f"- Tokens — input: {total_in:,} | cached read: {total_cache_read:,} | output: {total_out:,}",
        f"- Estimated cost: USD ${cost:.4f}",
        "",
        "## Finalist deep dives",
        "",
    ]
    for fr, report in finalists:
        sd = fr.sd
        lines.append(f"### {sd.ticker} — {sd.name}")
        if fr.margin_of_safety is not None:
            lines.append(f"_Stage 1 margin of safety: {fr.margin_of_safety*100:+.1f}%_")
        lines.append("")
        # Raw analyst consensus
        if sd.analyst_count or sd.analyst_mean_target:
            upside = ""
            if sd.analyst_mean_target and sd.price:
                upside = f" ({(sd.analyst_mean_target/sd.price - 1)*100:+.1f}% vs current)"
            lines.append("**Analyst consensus (raw):**")
            lines.append(
                f"- Rating: {sd.analyst_recommendation or 'n/a'} | "
                f"Mean target: {data.fmt_money(sd.analyst_mean_target, sd.currency)}{upside} | "
                f"{sd.analyst_count} analysts"
            )
            lines.append("")
        # Recent news headlines
        if sd.news:
            lines.append("**Recent news / developments:**")
            for n in sd.news[:8]:
                title = n.get("title", "").strip()
                pub = n.get("publisher", "")
                summary = (n.get("summary") or "").strip()
                line = f"- {title}"
                if pub:
                    line += f" _({pub})_"
                lines.append(line)
                if summary and summary.lower() not in title.lower():
                    lines.append(f"  > {summary[:240]}")
            lines.append("")
        lines.append("**Buffett-style analysis:**")
        lines.append("")
        lines.append(report)
        lines.append("")

    lines.append("## Stage 1 shortlist (passed quantitative filter)")
    lines.append("")
    for r in passers:
        mos = f"{r.margin_of_safety*100:+.1f}%" if r.margin_of_safety is not None else "n/a"
        roic = f", ROIC {r.sd.roic:.1f}%" if r.sd.roic is not None else ""
        roe_hist = ""
        if r.roe_mean is not None:
            roe_hist = f", ROE μ={r.roe_mean:.1f}% σ={r.roe_stdev:.1f}%" if r.roe_stdev is not None else ""
        method = f" [{r.intrinsic_method}]" if r.intrinsic_method not in ("none", "") else ""
        if r.sd.pe and r.sd.pb and r.sd.roe:
            lines.append(
                f"- **{r.sd.ticker}** ({r.sd.name or 'n/a'}) — MoS {mos}{method}, "
                f"P/E {r.sd.pe:.1f}, P/B {r.sd.pb:.2f}, ROE {r.sd.roe:.1f}%{roic}{roe_hist}"
            )
        else:
            lines.append(f"- **{r.sd.ticker}** ({r.sd.name or 'n/a'}) — MoS {mos}{method}")
    lines.append("")

    if triage:
        lines.append("## Stage 2 triage results")
        lines.append("")
        for fr, triage_text in triage:
            verdict = "PROMOTE" if "PROMOTE" in triage_text.upper() else "DROP"
            lines.append(f"### {fr.sd.ticker} ({fr.sd.name}) — {verdict}")
            lines.append("")
            # Strip the final "VERDICT:" line from the body — already in the header
            body_lines = [
                ln for ln in triage_text.splitlines()
                if not ln.strip().upper().startswith("VERDICT:")
            ]
            body = "\n".join(body_lines).strip()
            if body:
                lines.append(body)
            lines.append("")

    lines.append("## Stage 1 drops")
    lines.append("")
    for r in dropped:
        reasons = "; ".join(r.reasons) or "n/a"
        lines.append(f"- {r.sd.ticker} ({r.sd.name or 'n/a'}): {reasons}")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["sgx", "hk", "us"], default="sgx",
                    help="Market to screen. Picks universe_<market>.txt and market-specific prompt context.")
    ap.add_argument("--universe", default=None,
                    help="Override universe file path (defaults to universe_<market>.txt)")
    ap.add_argument("--max-shortlist", type=int, default=10)
    ap.add_argument("--max-finalists", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true", help="Stage 1 only, no LLM calls")
    args = ap.parse_args()

    market = args.market.upper()
    universe_path = args.universe or str(ROOT / f"universe_{args.market}.txt")

    load_dotenv()
    load_claude_settings_env()

    tickers = data.load_universe(universe_path)
    print(f"[1/4] Loaded {len(tickers)} {market} tickers from {universe_path}", file=sys.stderr)

    print(f"[2/4] Fetching financials...", file=sys.stderr)
    stocks = data.fetch_universe(tickers)

    print(f"[3/4] Stage 1 quantitative filter...", file=sys.stderr)
    results = filters.stage1(stocks)
    passers = [r for r in results if r.passed][: args.max_shortlist]
    print(f"      {len(passers)} passers / {len(results)}", file=sys.stderr)

    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"{dt.date.today().isoformat()}-{market.lower()}.md"

    if args.dry_run or not passers:
        write_report(out_path, market, len(tickers), results, [], [], [])
        print(f"[done] {out_path}", file=sys.stderr)
        return 0

    use_foundry = bool(os.getenv("ANTHROPIC_FOUNDRY_API_KEY") and os.getenv("ANTHROPIC_FOUNDRY_RESOURCE"))
    if use_foundry:
        from anthropic import AnthropicFoundry

        client = AnthropicFoundry()
        print(f"      using Foundry resource {os.getenv('ANTHROPIC_FOUNDRY_RESOURCE')}", file=sys.stderr)
    elif os.getenv("ANTHROPIC_API_KEY"):
        from anthropic import Anthropic

        client = Anthropic()
    else:
        print("ERROR: no ANTHROPIC credentials (set ANTHROPIC_API_KEY or Foundry vars)", file=sys.stderr)
        return 1
    records: list[dict] = []

    print(f"[4/4] Stage 2 Haiku triage on {len(passers)}...", file=sys.stderr)
    triage: list[tuple[filters.FilterResult, str]] = []
    promoted: list[filters.FilterResult] = []
    for fr in passers:
        bundle = data.compact_bundle(fr.sd)
        text, rec = haiku_triage(client, fr.sd.ticker, bundle, market)
        records.append(rec)
        triage.append((fr, text))
        if "PROMOTE" in text.upper():
            promoted.append(fr)
        print(f"      {fr.sd.ticker}: {'PROMOTE' if 'PROMOTE' in text.upper() else 'DROP'}",
              file=sys.stderr)

    promoted = promoted[: args.max_finalists]
    print(f"[5/5] Stage 3 Opus deep dive on {len(promoted)}...", file=sys.stderr)
    finalists: list[tuple[filters.FilterResult, str]] = []
    for fr in promoted:
        bundle = data.compact_bundle(fr.sd)
        text, rec = opus_deep_dive(client, fr.sd.ticker, bundle, market)
        records.append(rec)
        finalists.append((fr, text))
        print(f"      {fr.sd.ticker} done", file=sys.stderr)

    write_report(out_path, market, len(tickers), results, triage, finalists, records)
    cost = estimate_cost(records)
    print(f"[done] {out_path} (USD ${cost:.4f})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
