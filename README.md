# buffett-finder

A batch pipeline that finds undervalued stocks across **SGX / HK / US** in the style of Warren Buffett. Designed to keep token usage (and cost) low.

## Pipeline

1. **Stage 1 — Deterministic filter (no LLM):** ratios, intrinsic value, quality, consistency, liquidity. Drops obvious non-candidates for $0. See _Filter logic_ below.
2. **Stage 2 — Haiku triage:** rates shortlist on moat / management / financial health, PROMOTE or DROP.
3. **Stage 3 — Opus deep dive:** full Buffett-style report only on finalists.

Prompt caching is applied to the Buffett system prompt; the same block is reused across every call.

## Filter logic (Stage 1)

For every ticker we compute, from yfinance `info` + financial statements:

| Check | Non-financial | REIT | Bank |
|---|---|---|---|
| Market cap floor | SGD/USD-equiv per market | same | same |
| P/E | ≤ 25 | ≤ 25 | ≤ 25 |
| P/B | ≤ 2.5 | ≤ 1.3 | ≤ 1.5 |
| ROE (latest) | ≥ 10% | ≥ 5% | ≥ 8% |
| **ROIC** (NOPAT / IC) | **≥ 10%** | n/a | n/a |
| D/E | ≤ 1.0 | ≤ 1.0 | n/a |
| Positive FCF years | **4 of last 5** | n/a | n/a |
| **ROE consistency** | no declining trend, CoV ≤ 0.6 | n/a | no declining trend |
| **Margin of safety** | ≥ 15% vs **best of Graham or 10y DCF** | P/B proxy | P/B proxy |

**Owner-earnings DCF (new):** 10-year explicit FCF projection + Gordon-growth terminal. Growth = historical FCF CAGR clipped to `[0%, 8%]`. Discount rate 10%, terminal growth 2.5%. Base FCF = average of last 3 positive years. Per-share intrinsic = PV / shares outstanding.

**ROIC (new):** `NOPAT / (Total Debt + Equity)`, NOPAT = EBIT × (1 − effective tax rate). Soft check — only enforced when statements are available, so missing data doesn't auto-fail tickers.

**ROE consistency (new):** computed over all annual periods yfinance returns. A ticker fails if (a) the newer half's mean is < 70% of the older half's mean (declining trend) or (b) `stdev/mean > 0.6` (one-off-good-year cyclical). Skipped for REITs (revaluation noise).

The chosen intrinsic value uses the **higher** of Graham and DCF and is tagged `[graham]` or `[dcf]` in the report so you can see which one drove the verdict.

## Data layer

- **Day-keyed JSON cache:** `cache/YYYY-MM-DD/<ticker>.json`. Re-running the same day is free and instant. Delete a day's folder to force a refetch.
- **Data completeness score** logged per run — tickers under 60% complete are listed in stderr so you know which rejections were "fundamentals fail" vs "yfinance missing field."
- News falls back from yfinance to Google News RSS when yfinance returns nothing.

## Setup

```bash
cd buffett-finder
python -m venv .venv && source .venv/Scripts/activate  # Windows bash
pip install -r requirements.txt
cp .env.example .env  # paste your ANTHROPIC_API_KEY
```

## Run

```bash
python find.py --market sgx --dry-run        # Stage 1 only, $0 cost
python find.py --market sgx                  # full pipeline (default: sgx)
python find.py --market hk
python find.py --market us --max-finalists 3
```

Reports land in `reports/YYYY-MM-DD-<market>.md`. Per-call token usage is appended to `run_log.jsonl`.

## Editing the universe

Edit `universe_sgx.txt`, `universe_hk.txt`, or `universe_us.txt`. One ticker per line; `#` starts a comment (inline or full-line). SGX uses `.SI`, HK uses `.HK`, US uses no suffix.
