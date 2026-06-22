"""Data fetchers for SGX tickers. Returns compact text bundles to keep token usage low."""
from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import pathlib
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any

import feedparser
import yfinance as yf

CACHE_ROOT = pathlib.Path(__file__).parent / "cache"

# Fields used to compute data_completeness — these are the ones the Stage 1
# filter actually reads. Missing → 0, present → 1, averaged.
_COMPLETENESS_FIELDS = (
    "price", "market_cap", "pe", "pb", "roe", "debt_to_equity",
    "eps", "bvps", "fcf_positive_years", "shares_outstanding",
    "operating_margin", "interest_coverage",
)

# Suppress yfinance's noisy per-ticker HTTPError prints (delisted/renamed tickers)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


REIT_SUFFIX_HINTS = ("U.SI",)


@dataclass
class StockData:
    ticker: str
    name: str = ""
    sector: str = ""
    industry: str = ""
    currency: str = "SGD"
    market: str = "SGX"
    price: float | None = None
    market_cap: float | None = None
    pe: float | None = None
    pb: float | None = None
    roe: float | None = None
    debt_to_equity: float | None = None
    fcf_positive_years: int = 0
    fcf_history: list[float] = field(default_factory=list)  # most recent first, in reporting currency
    eps: float | None = None
    bvps: float | None = None
    shares_outstanding: float | None = None
    dividend_yield: float | None = None
    # Quality / consistency
    roe_history: list[float] = field(default_factory=list)  # percent, most recent first
    roic: float | None = None  # percent
    operating_margin: float | None = None  # percent
    interest_coverage: float | None = None  # EBIT / interest expense (x)
    peg: float | None = None  # PEG ratio (P/E divided by EPS growth)
    is_reit: bool = False
    is_bank: bool = False
    analyst_mean_target: float | None = None
    analyst_count: int = 0
    analyst_recommendation: str = ""
    news: list[dict] = field(default_factory=list)
    fetch_error: str = ""
    data_completeness: float = 0.0  # 0..1, fraction of filter-critical fields populated


def classify(ticker: str, info: dict) -> tuple[bool, bool]:
    industry = (info.get("industry") or "").lower()
    sector = (info.get("sector") or "").lower()
    longname = (info.get("longName") or "").upper()
    quote_type = (info.get("quoteType") or "").upper()
    is_bank = (
        "bank" in industry
        or industry in {"banks—regional", "banks—diversified", "banks - regional", "banks - diversified"}
        or "banking" in sector
    )
    is_reit = (
        "REIT" in longname
        or "REIT" in quote_type
        or "reit" in industry
        or "real estate investment trust" in industry
        or ticker.endswith(REIT_SUFFIX_HINTS)
    )
    return is_bank, is_reit


def detect_market(ticker: str) -> str:
    if ticker.endswith(".SI"):
        return "SGX"
    if ticker.endswith(".HK"):
        return "HK"
    return "US"


def _safe(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        if isinstance(v, str):
            try:
                v = float(v)
            except ValueError:
                continue
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            continue
        return v
    return None


def _num(v):
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _pos(v):
    """Treat zero or negative-or-missing as None — for ratios where 0 is a data quirk, not a real value."""
    n = _num(v)
    if n is None or n <= 0:
        return None
    return n


def _compute_roe_from_statements(t: yf.Ticker) -> float | None:
    """Compute ROE = Net Income / Avg Shareholders Equity from financial statements.
    Fallback when yfinance info['returnOnEquity'] is missing (common for SGX banks/REITs).
    Returns percent."""
    try:
        fin = t.financials
        bs = t.balance_sheet
        if fin is None or fin.empty or bs is None or bs.empty:
            return None
        ni = None
        for label in ("Net Income", "Net Income Common Stockholders", "Net Income From Continuing Operations"):
            if label in fin.index:
                ni = fin.loc[label].iloc[0]
                break
        eq = None
        for label in (
            "Stockholders Equity",
            "Total Stockholder Equity",
            "Common Stock Equity",
            "Total Equity Gross Minority Interest",
        ):
            if label in bs.index:
                series = bs.loc[label].dropna()
                if len(series) >= 2:
                    eq = (series.iloc[0] + series.iloc[1]) / 2
                elif len(series) == 1:
                    eq = series.iloc[0]
                break
        if ni is None or eq is None or eq == 0 or math.isnan(ni) or math.isnan(eq):
            return None
        return float(ni) / float(eq) * 100
    except Exception:
        return None


def _count_positive_fcf_years(t: yf.Ticker) -> int:
    try:
        cf = t.cashflow
        if cf is None or cf.empty:
            return 0
        fcf_row = None
        for label in ("Free Cash Flow", "FreeCashFlow"):
            if label in cf.index:
                fcf_row = cf.loc[label]
                break
        if fcf_row is None:
            ops = cf.loc["Operating Cash Flow"] if "Operating Cash Flow" in cf.index else None
            capex = cf.loc["Capital Expenditure"] if "Capital Expenditure" in cf.index else None
            if ops is None or capex is None:
                return 0
            fcf_row = ops + capex
        return int(sum(1 for v in fcf_row.values if v is not None and not math.isnan(v) and v > 0))
    except Exception:
        return 0


def _fcf_history(t: yf.Ticker) -> list[float]:
    """Return FCF for available annual periods, most recent first."""
    try:
        cf = t.cashflow
        if cf is None or cf.empty:
            return []
        fcf_row = None
        for label in ("Free Cash Flow", "FreeCashFlow"):
            if label in cf.index:
                fcf_row = cf.loc[label]
                break
        if fcf_row is None:
            ops = cf.loc["Operating Cash Flow"] if "Operating Cash Flow" in cf.index else None
            capex = cf.loc["Capital Expenditure"] if "Capital Expenditure" in cf.index else None
            if ops is None or capex is None:
                return []
            fcf_row = ops + capex
        # yfinance cashflow is sorted most-recent-first across columns
        vals = [float(v) for v in fcf_row.values if v is not None and not (isinstance(v, float) and math.isnan(v))]
        return vals
    except Exception:
        return []


def _roe_history(t: yf.Ticker) -> list[float]:
    """Per-year ROE % from financials/balance sheet, most recent first.
    Uses net income / shareholders equity (single-year, not averaged — keeps periods aligned)."""
    try:
        fin = t.financials
        bs = t.balance_sheet
        if fin is None or fin.empty or bs is None or bs.empty:
            return []
        ni_row = None
        for label in ("Net Income", "Net Income Common Stockholders", "Net Income From Continuing Operations"):
            if label in fin.index:
                ni_row = fin.loc[label]
                break
        eq_row = None
        for label in (
            "Stockholders Equity", "Total Stockholder Equity",
            "Common Stock Equity", "Total Equity Gross Minority Interest",
        ):
            if label in bs.index:
                eq_row = bs.loc[label]
                break
        if ni_row is None or eq_row is None:
            return []
        out: list[float] = []
        for col in ni_row.index:
            if col not in eq_row.index:
                continue
            ni = ni_row[col]
            eq = eq_row[col]
            if ni is None or eq is None or eq == 0:
                continue
            if isinstance(ni, float) and math.isnan(ni):
                continue
            if isinstance(eq, float) and (math.isnan(eq) or eq <= 0):
                continue
            out.append(float(ni) / float(eq) * 100)
        return out
    except Exception:
        return []


def _compute_roic(t: yf.Ticker, info: dict) -> float | None:
    """ROIC = NOPAT / (Total Debt + Equity), percent. Most recent annual period."""
    try:
        fin = t.financials
        bs = t.balance_sheet
        if fin is None or fin.empty or bs is None or bs.empty:
            return None
        ebit = None
        for label in ("EBIT", "Operating Income", "Earnings Before Interest And Taxes"):
            if label in fin.index:
                ebit = fin.loc[label].iloc[0]
                break
        if ebit is None or (isinstance(ebit, float) and math.isnan(ebit)):
            return None
        # tax rate: pull from income statement if possible
        tax_rate = 0.22
        if "Tax Provision" in fin.index and "Pretax Income" in fin.index:
            tp = fin.loc["Tax Provision"].iloc[0]
            pti = fin.loc["Pretax Income"].iloc[0]
            if pti and not math.isnan(pti) and pti > 0 and tp is not None and not math.isnan(tp):
                tax_rate = max(0.0, min(0.5, float(tp) / float(pti)))
        nopat = float(ebit) * (1 - tax_rate)
        # Invested capital: total debt + equity
        debt = None
        for label in ("Total Debt", "Long Term Debt"):
            if label in bs.index:
                debt = bs.loc[label].iloc[0]
                break
        eq = None
        for label in (
            "Stockholders Equity", "Total Stockholder Equity",
            "Common Stock Equity", "Total Equity Gross Minority Interest",
        ):
            if label in bs.index:
                eq = bs.loc[label].iloc[0]
                break
        if eq is None or (isinstance(eq, float) and math.isnan(eq)) or eq <= 0:
            return None
        debt_val = 0.0 if (debt is None or (isinstance(debt, float) and math.isnan(debt))) else float(debt)
        ic = debt_val + float(eq)
        if ic <= 0:
            return None
        return nopat / ic * 100
    except Exception:
        return None


def _interest_coverage(t: yf.Ticker) -> float | None:
    """EBIT / Interest Expense from latest annual income statement. Returns x (multiple)."""
    try:
        fin = t.financials
        if fin is None or fin.empty:
            return None
        ebit = None
        for label in ("EBIT", "Operating Income", "Earnings Before Interest And Taxes"):
            if label in fin.index:
                ebit = fin.loc[label].iloc[0]
                break
        intx = None
        for label in ("Interest Expense", "Interest Expense Non Operating", "Net Interest Income"):
            if label in fin.index:
                intx = fin.loc[label].iloc[0]
                break
        if ebit is None or intx is None:
            return None
        if isinstance(ebit, float) and math.isnan(ebit):
            return None
        if isinstance(intx, float) and math.isnan(intx):
            return None
        intx = abs(float(intx))  # yfinance sometimes signs negative
        if intx == 0:
            return None  # no debt → coverage is infinite; let other checks handle leverage
        return float(ebit) / intx
    except Exception:
        return None


def _completeness(sd: "StockData") -> float:
    present = sum(1 for name in _COMPLETENESS_FIELDS if getattr(sd, name) not in (None, 0, 0.0))
    return present / len(_COMPLETENESS_FIELDS)


def _cache_path(ticker: str) -> pathlib.Path:
    day = _dt.date.today().isoformat()
    return CACHE_ROOT / day / f"{ticker.replace('/', '_')}.json"


def _load_cached(ticker: str) -> "StockData | None":
    p = _cache_path(ticker)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        valid = {f.name for f in fields(StockData)}
        return StockData(**{k: v for k, v in raw.items() if k in valid})
    except Exception:
        return None


def _save_cached(sd: "StockData") -> None:
    try:
        p = _cache_path(sd.ticker)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(sd), default=str), encoding="utf-8")
    except Exception:
        pass


def fetch_stock(ticker: str) -> StockData:
    sd = StockData(ticker=ticker, market=detect_market(ticker))
    default_ccy = {"SGX": "SGD", "HK": "HKD", "US": "USD"}.get(sd.market, "USD")
    try:
        t = yf.Ticker(ticker)
        # Some delisted/invalid tickers return an empty info dict or raise inside .info
        try:
            info = t.info or {}
        except Exception:
            info = {}
        if not info or not (info.get("longName") or info.get("shortName") or info.get("regularMarketPrice")):
            sd.fetch_error = "no data (delisted, invalid, or unsupported by yfinance)"
            return sd
        sd.name = info.get("longName") or info.get("shortName") or ticker
        sd.sector = info.get("sector") or ""
        sd.industry = info.get("industry") or ""
        sd.currency = info.get("currency") or default_ccy
        sd.price = _safe(info, "currentPrice", "regularMarketPrice", "previousClose")
        sd.market_cap = _pos(info.get("marketCap"))
        sd.pe = _safe(info, "trailingPE", "forwardPE")
        sd.pb = _pos(info.get("priceToBook"))
        roe = _num(info.get("returnOnEquity"))
        sd.roe = roe * 100 if roe is not None else None
        if sd.roe is None:
            sd.roe = _compute_roe_from_statements(t)
        de = _num(info.get("debtToEquity"))
        sd.debt_to_equity = de / 100 if de is not None and de > 5 else de
        sd.eps = _safe(info, "trailingEps", "forwardEps")
        sd.bvps = _pos(info.get("bookValue"))
        dy = _num(info.get("dividendYield"))
        sd.dividend_yield = dy * 100 if dy is not None and dy < 1 else dy
        sd.shares_outstanding = _pos(info.get("sharesOutstanding"))
        sd.is_bank, sd.is_reit = classify(ticker, info)
        sd.fcf_positive_years = _count_positive_fcf_years(t)
        sd.fcf_history = _fcf_history(t)
        sd.roe_history = _roe_history(t)
        om = _num(info.get("operatingMargins"))
        sd.operating_margin = om * 100 if om is not None else None
        sd.peg = _num(info.get("trailingPegRatio")) or _num(info.get("pegRatio"))
        if not (sd.is_bank or sd.is_reit):
            sd.roic = _compute_roic(t, info)
            sd.interest_coverage = _interest_coverage(t)

        # analyst sentiment
        try:
            rec = info.get("recommendationKey") or ""
            sd.analyst_recommendation = rec
            sd.analyst_mean_target = _num(info.get("targetMeanPrice"))
            sd.analyst_count = _num(info.get("numberOfAnalystOpinions")) or 0
        except Exception:
            pass

        # news (yfinance)
        try:
            raw_news = (t.news or [])[:8]
            for n in raw_news:
                content = n.get("content") or n
                title = content.get("title") if isinstance(content, dict) else n.get("title")
                pub = (
                    content.get("provider", {}).get("displayName")
                    if isinstance(content, dict)
                    else n.get("publisher")
                )
                summary = content.get("summary") if isinstance(content, dict) else ""
                if title:
                    sd.news.append(
                        {"title": title, "publisher": pub or "", "summary": (summary or "")[:200]}
                    )
        except Exception:
            pass

        if not sd.news:
            sd.news = _google_news_fallback(sd.name, sd.market)

    except Exception as e:
        sd.fetch_error = str(e)
    return sd


def _google_news_fallback(company: str, market: str = "SGX", limit: int = 5) -> list[dict]:
    try:
        locale = {
            "SGX": ("en-SG", "SG", "SG:en", "SGX"),
            "HK": ("en-HK", "HK", "HK:en", "HKEX"),
            "US": ("en-US", "US", "US:en", "stock"),
        }.get(market, ("en-US", "US", "US:en", "stock"))
        hl, gl, ceid, tag = locale
        q = f"{company} {tag}"
        url = f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
        feed = feedparser.parse(url)
        out = []
        for entry in feed.entries[:limit]:
            out.append(
                {
                    "title": entry.get("title", ""),
                    "publisher": entry.get("source", {}).get("title", "")
                    if isinstance(entry.get("source"), dict)
                    else "",
                    "summary": (entry.get("summary") or "")[:200],
                }
            )
        return out
    except Exception:
        return []


def fmt_money(v: float | None, currency: str = "SGD") -> str:
    if v is None:
        return "n/a"
    if abs(v) >= 1e9:
        return f"{currency} {v/1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"{currency} {v/1e6:.2f}M"
    return f"{currency} {v:,.2f}"


def fmt_num(v: float | None, suffix: str = "") -> str:
    if v is None:
        return "n/a"
    return f"{v:.2f}{suffix}"


def compact_bundle(sd: StockData) -> str:
    """Render a stock as a terse plain-text bundle (~400-600 tokens)."""
    kind = "REIT" if sd.is_reit else "Bank" if sd.is_bank else "Company"
    upside = ""
    if sd.analyst_mean_target and sd.price:
        upside = f" ({(sd.analyst_mean_target/sd.price - 1)*100:+.1f}% upside)"
    lines = [
        f"Ticker: {sd.ticker} ({kind})",
        f"Name: {sd.name}",
        f"Sector/Industry: {sd.sector} / {sd.industry}",
        f"Currency: {sd.currency}",
        f"Price: {fmt_money(sd.price, sd.currency)}",
        f"Market cap: {fmt_money(sd.market_cap, sd.currency)}",
        f"P/E: {fmt_num(sd.pe)}  P/B: {fmt_num(sd.pb)}  PEG: {fmt_num(sd.peg)}",
        f"ROE: {fmt_num(sd.roe, '%')}  ROIC: {fmt_num(sd.roic, '%')}  Operating margin: {fmt_num(sd.operating_margin, '%')}",
        f"Debt/Equity: {fmt_num(sd.debt_to_equity)}  Interest coverage: {fmt_num(sd.interest_coverage, 'x')}  Dividend yield: {fmt_num(sd.dividend_yield, '%')}",
        f"EPS: {fmt_num(sd.eps)}  BVPS: {fmt_num(sd.bvps)}",
        f"Positive FCF years (last 5): {sd.fcf_positive_years}",
        f"ROE history (most recent first, %): "
        + (", ".join(f"{r:.1f}" for r in sd.roe_history[:5]) if sd.roe_history else "n/a"),
        f"FCF history ({sd.currency}, most recent first): "
        + (", ".join(fmt_money(v, sd.currency) for v in sd.fcf_history[:5]) if sd.fcf_history else "n/a"),
        f"Analyst consensus: {sd.analyst_recommendation or 'n/a'} | "
        f"target {fmt_money(sd.analyst_mean_target, sd.currency)}{upside} | "
        f"{sd.analyst_count} analysts",
    ]
    if sd.news:
        lines.append("Recent news:")
        for n in sd.news[:8]:
            t = n["title"][:120]
            p = n["publisher"]
            lines.append(f"  - {t} ({p})")
    return "\n".join(lines)


def fetch_universe(
    tickers: list[str],
    delay: float = 0.3,
    verbose: bool = False,
    use_cache: bool = True,
) -> list[StockData]:
    out = []
    skipped = []
    low_completeness = []
    cache_hits = 0
    for tk in tickers:
        sd: StockData | None = _load_cached(tk) if use_cache else None
        if sd is None:
            sd = fetch_stock(tk)
            sd.data_completeness = _completeness(sd)
            if not sd.fetch_error:
                _save_cached(sd)
            time.sleep(delay)
        else:
            cache_hits += 1
        out.append(sd)
        if sd.fetch_error:
            skipped.append(tk)
        elif sd.data_completeness < 0.6:
            low_completeness.append(f"{tk}({sd.data_completeness:.0%})")
    if cache_hits:
        print(f"      cache hits: {cache_hits}/{len(tickers)}", file=sys.stderr)
    if skipped:
        print(f"      skipped {len(skipped)} tickers with no data: {', '.join(skipped)}", file=sys.stderr)
    if low_completeness:
        print(
            f"      low data completeness ({len(low_completeness)}): {', '.join(low_completeness[:15])}"
            + (" …" if len(low_completeness) > 15 else ""),
            file=sys.stderr,
        )
    return out


def load_universe(path: str) -> list[str]:
    tickers = []
    with open(path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            tickers.append(line)
    seen = set()
    result = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result
