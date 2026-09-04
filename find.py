"""Buffett-style SGX undervalued stock finder. Batch pipeline."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> None:
        return None

import data
import filters
import lenses
import prompts
import state


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
CURSOR_TRIAGE_MODEL = os.getenv("BUFFETT_CURSOR_TRIAGE_MODEL", "composer-2.5")
CURSOR_DIVE_MODEL = os.getenv("BUFFETT_CURSOR_DIVE_MODEL", "composer-2.5")

# Approx pricing (USD per 1M tokens) for Anthropic cost estimation
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
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_input_tokens": (
            getattr(usage, "cache_read_input_tokens", None)
            or getattr(usage, "cache_read_tokens", 0)
            or 0
        ),
        "cache_creation_input_tokens": (
            getattr(usage, "cache_creation_input_tokens", None)
            or getattr(usage, "cache_write_tokens", 0)
            or 0
        ),
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


def _cursor_model(model_id: str, *, fast: bool = False):
    from cursor_sdk import ModelParameterValue, ModelSelection

    params = [ModelParameterValue(id="fast", value="true")] if fast else ()
    return ModelSelection(id=model_id, params=params)


def cursor_complete(stage: str, ticker: str, system: str, user: str, model_id: str, *, fast: bool = False) -> tuple[str, dict]:
    """One-shot Cursor SDK call, text-only (no repo tools)."""
    from cursor_sdk import Agent, AgentOptions, CursorAgentError, LocalAgentOptions

    options = AgentOptions(
        api_key=os.environ.get("CURSOR_API_KEY"),
        model=_cursor_model(model_id, fast=fast),
        tools=[],
        local=LocalAgentOptions(cwd=str(ROOT)),
    )
    try:
        result = Agent.prompt(f"{system}\n\n{user}", options)
    except CursorAgentError as err:
        raise RuntimeError(f"Cursor {stage} failed to start for {ticker}: {err.message}") from err
    if getattr(result, "status", None) == "error":
        raise RuntimeError(f"Cursor {stage} run failed for {ticker}: {getattr(result, 'id', '')}")
    text = (getattr(result, "result", None) or "").strip()
    usage = getattr(result, "usage", None) or object()
    rec = log_usage(stage, ticker, f"{model_id}{'-fast' if fast else ''}", usage)
    return text, rec


def haiku_triage(client, ticker: str, bundle: str, market: str, backend: str) -> tuple[str, dict]:
    user = f"Data bundle:\n\n{bundle}\n\n{prompts.HAIKU_TRIAGE_TASK}"
    if backend == "cursor":
        return cursor_complete(
            "triage", ticker, prompts.system_prompt(market), user,
            CURSOR_TRIAGE_MODEL, fast=True,
        )
    msg = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=400,
        system=cached_system_block(market),
        messages=[{"role": "user", "content": user}],
    )
    rec = log_usage("triage", ticker, HAIKU_MODEL, msg.usage)
    return extract_text(msg), rec


def opus_deep_dive(client, ticker: str, bundle: str, market: str, lens: dict, backend: str) -> tuple[str, dict]:
    user = f"Data bundle:\n\n{bundle}\n\n{prompts.opus_deep_dive_task(lens)}"
    if backend == "cursor":
        return cursor_complete(
            "deep_dive", ticker, prompts.system_prompt(market), user,
            CURSOR_DIVE_MODEL, fast=False,
        )
    msg = client.messages.create(
        model=OPUS_MODEL,
        max_tokens=2000,
        system=cached_system_block(market),
        messages=[{"role": "user", "content": user}],
    )
    rec = log_usage("deep_dive", ticker, OPUS_MODEL, msg.usage)
    return extract_text(msg), rec


def write_report(
    out_path: pathlib.Path,
    market: str,
    universe_size: int,
    stage1_results: list[filters.FilterResult],
    shortlist: list[filters.FilterResult],
    triage: list[tuple[filters.FilterResult, str]],
    finalists: list[tuple[filters.FilterResult, str]],
    records: list[dict],
    delta: state.MarketDelta,
    lens: dict,
    wildcard: filters.FilterResult | None,
) -> None:
    dropped = [r for r in stage1_results if not r.passed]
    total_in = sum(r["input_tokens"] for r in records)
    total_out = sum(r["output_tokens"] for r in records)
    total_cache_read = sum(r["cache_read_input_tokens"] for r in records)
    cost = estimate_cost(records)

    def _mos(fr: filters.FilterResult) -> str:
        return f"{fr.margin_of_safety*100:+.1f}%" if fr.margin_of_safety is not None else "n/a"

    def _score(fr: filters.FilterResult) -> str:
        return f"{fr.composite:.0f}" if fr.composite is not None else "n/a"

    lines = [
        f"# {market} Buffett Finder Report — {dt.date.today().isoformat()}",
        "",
        f"**Lens this week:** {lens['title']}",
        "",
        "## What changed",
        "",
    ]
    if delta.first_snapshot:
        lines.append("_Baseline snapshot — no prior run to diff. Subsequent runs lead with new / dropped / moved._")
        lines.append("")
    else:
        lines.append(
            f"- New: {', '.join(delta.new) or '—'} · "
            f"Returned: {', '.join(delta.returned) or '—'} · "
            f"Moved: {', '.join(delta.moved) or '—'} · "
            f"Stale (still here): {', '.join(delta.stale) or '—'} · "
            f"Dropped: {', '.join(delta.dropped) or '—'}"
        )
        lines.append("")

    lines += [
        "## Pipeline summary",
        f"- Universe size: {universe_size}",
        f"- Stage 1 passers: {sum(1 for r in stage1_results if r.passed)}",
        f"- Shortlist (composite + sector cap): {len(shortlist)}",
        f"- Stage 2 triage promotions: {len(finalists)}",
        f"- Stage 3 deep dives (new/moved/refresh only): {len(finalists)}",
        f"- Tokens — input: {total_in:,} | cached read: {total_cache_read:,} | output: {total_out:,}",
        f"- Estimated cost: USD ${cost:.4f}",
        "",
        "## Finalist deep dives",
        "",
    ]
    if not finalists:
        lines.append("_No deep dives this run — shortlist is stale and nothing moved enough to rewrite._")
        lines.append("")
    for fr, report in finalists:
        sd = fr.sd
        d = delta.by_ticker.get(sd.ticker)
        tag = d.status.upper() if d else ""
        lines.append(f"### {sd.ticker} — {sd.name} ({tag})")
        meta = f"_Score {_score(fr)} · Stage 1 MoS {_mos(fr)}"
        if fr.intrinsic_method not in ("none", ""):
            meta += f" [{fr.intrinsic_method}]"
        if sd.pct_from_high is not None:
            meta += f" · {sd.pct_from_high*100:+.0f}% vs 52w high"
        meta += "_"
        lines.append(meta)
        lines.append("")
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
        lines.append("**Analysis:**")
        lines.append("")
        lines.append(report)
        lines.append("")

    lines.append("## Shortlist (composite rank, max 2 per sector/group)")
    lines.append("")
    for r in shortlist:
        d = delta.by_ticker.get(r.sd.ticker)
        status = d.status if d else ""
        streak = f" day {d.streak}" if d else ""
        mos = _mos(r)
        method = f" [{r.intrinsic_method}]" if r.intrinsic_method not in ("none", "") else ""
        from_high = f", {r.sd.pct_from_high*100:+.0f}% vs 52w" if r.sd.pct_from_high is not None else ""
        if r.sd.pe and r.sd.pb and r.sd.roe:
            lines.append(
                f"- **{r.sd.ticker}** ({r.sd.name or 'n/a'}) — {status}{streak}, "
                f"score {_score(r)}, MoS {mos}{method}{from_high}, "
                f"P/E {r.sd.pe:.1f}, P/B {r.sd.pb:.2f}, ROE {r.sd.roe:.1f}%"
            )
        else:
            lines.append(
                f"- **{r.sd.ticker}** ({r.sd.name or 'n/a'}) — {status}{streak}, "
                f"score {_score(r)}, MoS {mos}{method}{from_high}"
            )
    lines.append("")

    if wildcard:
        w = wildcard
        lines.append("## Wildcard (near-miss the hard filter dropped)")
        lines.append("")
        lines.append(
            f"- **{w.sd.ticker}** ({w.sd.name or 'n/a'}) — score {_score(w)}, "
            f"failed: {'; '.join(w.reasons)}"
        )
        lines.append("")

    if triage:
        lines.append("## Stage 2 triage results")
        lines.append("")
        for fr, triage_text in triage:
            verdict = "PROMOTE" if "PROMOTE" in triage_text.upper() else "DROP"
            lines.append(f"### {fr.sd.ticker} ({fr.sd.name}) — {verdict}")
            lines.append("")
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
    ap.add_argument("--lens", default=None, choices=[ln["id"] for ln in lenses.LENSES],
                    help="Override the weekly rotating analysis lens")
    ap.add_argument("--backend", choices=["auto", "cursor", "anthropic"], default="auto",
                    help="LLM backend. auto = Cursor if CURSOR_API_KEY is set, else Anthropic.")
    ap.add_argument("--dry-run", action="store_true", help="Stage 1 only, no LLM calls")
    args = ap.parse_args()

    market = args.market.upper()
    universe_path = args.universe or str(ROOT / f"universe_{args.market}.txt")
    lens = lenses.current_lens(lens_id=args.lens)

    load_dotenv()
    load_claude_settings_env()

    tickers = data.load_universe(universe_path)
    print(f"[1/4] Loaded {len(tickers)} {market} tickers from {universe_path}", file=sys.stderr)
    print(f"      lens: {lens['title']}", file=sys.stderr)

    print(f"[2/4] Fetching financials...", file=sys.stderr)
    stocks = data.fetch_universe(tickers)

    print(f"[3/4] Stage 1 quantitative filter...", file=sys.stderr)
    results = filters.stage1(stocks)
    shortlist = filters.pick_shortlist(results, args.max_shortlist)
    wildcard = filters.pick_wildcard(results, shortlist)
    delta = state.diff(market, shortlist)
    print(
        f"      {sum(1 for r in results if r.passed)} passers / {len(results)} → "
        f"shortlist {len(shortlist)}"
        + (f" + wildcard {wildcard.sd.ticker}" if wildcard else ""),
        file=sys.stderr,
    )
    if not delta.first_snapshot:
        print(
            f"      delta: new={delta.new} moved={delta.moved} "
            f"returned={delta.returned} dropped={delta.dropped}",
            file=sys.stderr,
        )

    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"{dt.date.today().isoformat()}-{market.lower()}.md"

    if args.dry_run or not shortlist:
        write_report(
            out_path, market, len(tickers), results, shortlist,
            [], [], [], delta, lens, wildcard,
        )
        if not args.dry_run:
            state.save(market, shortlist)
        print(f"[done] {out_path}", file=sys.stderr)
        return 0

    backend = args.backend
    if backend == "auto":
        backend = "cursor" if os.getenv("CURSOR_API_KEY") else "anthropic"

    client = None
    if backend == "cursor":
        if not os.getenv("CURSOR_API_KEY"):
            print("ERROR: CURSOR_API_KEY is not set", file=sys.stderr)
            return 1
        print(
            f"      backend=cursor  triage={CURSOR_TRIAGE_MODEL}-fast  "
            f"deep-dive={CURSOR_DIVE_MODEL}",
            file=sys.stderr,
        )
    else:
        use_foundry = bool(os.getenv("ANTHROPIC_FOUNDRY_API_KEY") and os.getenv("ANTHROPIC_FOUNDRY_RESOURCE"))
        if use_foundry:
            from anthropic import AnthropicFoundry

            client = AnthropicFoundry()
            print(f"      backend=anthropic Foundry {os.getenv('ANTHROPIC_FOUNDRY_RESOURCE')}", file=sys.stderr)
        elif os.getenv("ANTHROPIC_API_KEY"):
            from anthropic import Anthropic

            client = Anthropic()
            print("      backend=anthropic", file=sys.stderr)
        else:
            print("ERROR: no ANTHROPIC credentials (set ANTHROPIC_API_KEY or Foundry vars)", file=sys.stderr)
            return 1
    records: list[dict] = []

    dive_candidates = state.pick_deep_dives(shortlist, delta, args.max_finalists)
    print(
        f"[4/4] Stage 2 triage on {len(dive_candidates)} "
        f"(skipped {len(shortlist) - len(dive_candidates)} stale)...",
        file=sys.stderr,
    )
    triage: list[tuple[filters.FilterResult, str]] = []
    promoted: list[filters.FilterResult] = []
    for fr in dive_candidates:
        extra = state.extra_bundle_lines(fr, delta, lens["title"])
        bundle = data.compact_bundle(fr.sd, extra)
        text, rec = haiku_triage(client, fr.sd.ticker, bundle, market, backend)
        records.append(rec)
        triage.append((fr, text))
        if "PROMOTE" in text.upper():
            promoted.append(fr)
        print(f"      {fr.sd.ticker}: {'PROMOTE' if 'PROMOTE' in text.upper() else 'DROP'}",
              file=sys.stderr)

    promoted = promoted[: args.max_finalists]
    print(f"[5/5] Stage 3 deep dive on {len(promoted)}...", file=sys.stderr)
    finalists: list[tuple[filters.FilterResult, str]] = []
    for fr in promoted:
        extra = state.extra_bundle_lines(fr, delta, lens["title"])
        bundle = data.compact_bundle(fr.sd, extra)
        text, rec = opus_deep_dive(client, fr.sd.ticker, bundle, market, lens, backend)
        records.append(rec)
        finalists.append((fr, text))
        print(f"      {fr.sd.ticker} done", file=sys.stderr)

    write_report(
        out_path, market, len(tickers), results, shortlist,
        triage, finalists, records, delta, lens, wildcard,
    )
    state.save(market, shortlist, deep_dived=[fr.sd.ticker for fr, _ in finalists])
    cost = estimate_cost(records)
    print(f"[done] {out_path} (USD ${cost:.4f})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
