# buffett-finder

A batch pipeline that finds undervalued stocks across **SGX / HK / US** in the style of Warren Buffett. Designed to keep token usage (and cost) low, and to **not** send the same five names with the same essay every morning.

## Pipeline

1. **Stage 1 — Deterministic filter (no LLM):** ratios, conservative intrinsic value, quality, consistency, liquidity.
2. **Rank & diversify:** composite score (cheapness × quality × setup) with a max of 2 names per sector/group, plus one beaten-up near-miss wildcard.
3. **Delta vs last run:** new / returned / moved / stale / dropped. Stale names are one-liners, not a rewrite.
4. **Stage 2 — Haiku triage:** only on new, moved, returned, or names not deep-dived in 14 days. Stale names with nothing new are dropped.
5. **Stage 3 — Opus deep dive:** rotating weekly lens (inversion / owner / capital-cycle / relative) plus a mandatory bear case. Not the old 9-section brochure.

Prompt caching is applied to the Buffett system prompt; the same block is reused across every call. Run state lives in `state/<market>.json` (gitignored; restored on GitHub Actions via cache).

## Filter logic (Stage 1)

For every ticker we compute, from yfinance `info` + financial statements:

| Check | Non-financial | REIT | Bank |
|---|---|---|---|
| Market cap floor | SGD/USD-equiv per market | same | same |
| P/E | ≤ 25 (35 if PEG&lt;1.5 and ROE&gt;20%) | ≤ 25 | ≤ 25 |
| P/B | ≤ 2.5 | ≤ 1.3 | ≤ 1.5 |
| ROE (latest) | ≥ 10% | ≥ 5% | ≥ 8% |
| **ROIC** | **≥ 10%** | n/a | n/a |
| D/E | ≤ 1.0 | ≤ 1.0 | n/a |
| Positive FCF years | **4 of last 5** | n/a | n/a |
| Operating margin | hard floor **8%** (8–15% is a quality penalty, not a drop) | n/a | n/a |
| Interest coverage | ≥ 5x | n/a | n/a |
| **ROE consistency** | no declining trend, CoV ≤ 0.6 | n/a | no declining trend |
| **Margin of safety** | ≥ 15% vs **conservative (lower of Graham or 10y DCF)** | ranking only (P/B + yield) | ranking only (justified P/B) |

**Owner-earnings DCF:** 10-year explicit FCF projection + Gordon-growth terminal. Growth = historical FCF CAGR clipped to `[0%, 8%]`. Discount rate 10%, terminal growth 2.5%. Base FCF = average of last 3 positive years. Per-share intrinsic = PV / shares outstanding.

**Conservative IV:** when both Graham and DCF exist, use the **lower** number. The old pipeline took the max, which inflated margin of safety.

**Banks:** justified P/B = ROE / 10% cost of equity. **REITs:** blend of P/B vs cap and dividend yield vs 6% required yield.

**ROIC:** `NOPAT / (Total Debt + Equity)`, NOPAT = EBIT × (1 − effective tax rate). Soft check — only enforced when statements are available.

**Composite rank (0–100):** 35% cheapness (MoS), 40% quality (ROE/ROIC/consistency/margin), 25% setup (drawdown vs 52-week high, 1y return). Shortlist is this rank with a **max 2 per sector / REIT / bank**.

## Data layer

- **Day-keyed JSON cache:** `cache/YYYY-MM-DD/<ticker>.json`. Re-running the same day is free and instant. Delete a day's folder to force a refetch.
- **Setup fields:** 52-week range, % from high, 1y return, SMA200, beta, next earnings date.
- **Data completeness score** logged per run — tickers under 60% complete are listed in stderr.
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
python find.py --market sgx                  # full pipeline (auto: Cursor if CURSOR_API_KEY, else Anthropic)
python find.py --market sgx --backend cursor # Composer 2.5 triage (fast) + deep dive
python find.py --market hk --lens inversion  # force this week's lens
python find.py --market us --max-finalists 3
python -m unittest test_filters.py           # ranking / IV / delta tests
```

Reports land in `reports/YYYY-MM-DD-<market>.md`. Per-call token usage is appended to `run_log.jsonl`.

Weekly lenses rotate by ISO week: `inversion` → `owner` → `capital-cycle` → `relative`. Override with `--lens`.

## Editing the universe

Edit `universe_sgx.txt`, `universe_hk.txt`, or `universe_us.txt`. One ticker per line; `#` starts a comment (inline or full-line). SGX uses `.SI`, HK uses `.HK`, US uses no suffix.
