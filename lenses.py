"""Rotating analysis lenses so Stage 3 reports are not the same essay every week."""
from __future__ import annotations

import datetime as dt


LENSES: list[dict[str, str]] = [
    {
        "id": "inversion",
        "title": "Inversion — how is this a value trap?",
        "task": (
            "This week's mandated lens is INVERSION. Spend the bulk of the analysis "
            "on how this idea fails. What would have to be true for today's cheapness "
            "to be a trap (melting ice cube, accounting mirage, capital cycle peak, "
            "regulatory cliff, key-man, hidden leverage)? Write the kill criterion "
            "as a falsifiable sentence. Only then argue why that bear case is or isn't "
            "the base case. Do not produce a balanced 9-section brochure."
        ),
    },
    {
        "id": "owner",
        "title": "Owner mindset — would you buy the whole firm?",
        "task": (
            "This week's mandated lens is OWNER-OPERATOR. Ignore the ticker. At today's "
            "enterprise value, would you buy 100% of this business with 10-year money "
            "and no ability to sell? Focus on capital allocation (buybacks vs empire "
            "building vs dividends vs reinvestment ROIC), insider incentives, and "
            "whether owner earnings can compound. If you would not buy the whole thing, "
            "the stock is a PASS regardless of the multiple."
        ),
    },
    {
        "id": "capital-cycle",
        "title": "Capital cycle — industry supply, not the last print",
        "task": (
            "This week's mandated lens is THE CAPITAL CYCLE. Where is this industry "
            "in the capacity/capex cycle? Are competitors still adding supply, or has "
            "capex collapsed? What does that imply for pricing power and ROIC over "
            "5–10 years? Recency of a good (or bad) year is a trap — reason from "
            "industry supply, not last year's earnings."
        ),
    },
    {
        "id": "relative",
        "title": "Relative value — cheap stock or cheap sector?",
        "task": (
            "This week's mandated lens is RELATIVE VALUE. Is this name cheap because "
            "the whole sector is hated, or because THIS franchise is impaired? "
            "Compare implied expectations vs a generic peer (use sector knowledge, "
            "not invented peer financials). If the sector is cheap, say what would "
            "make you pick this name over a sector ETF. If the sector is fine and "
            "this name is the discount, say why the market singled it out."
        ),
    },
]


def current_lens(when: dt.date | None = None, lens_id: str | None = None) -> dict[str, str]:
    if lens_id:
        for ln in LENSES:
            if ln["id"] == lens_id:
                return ln
        raise ValueError(f"unknown lens {lens_id!r}; choose from {[x['id'] for x in LENSES]}")
    when = when or dt.date.today()
    return LENSES[when.isocalendar()[1] % len(LENSES)]
