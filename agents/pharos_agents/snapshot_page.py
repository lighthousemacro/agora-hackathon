"""Bake web/lib/snapshot.ts from the pricing engine.

The public board reads crowd price, the posted framework price, and
resolution from the chain in the browser. This file is the model writeup
(pillar, IC, reasoning) and the fallback when that read fails. Re-baking
replaces those words with the current information state and, when a chain
is reachable, the live prices. It never replaces the board with an empty
list.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "pricing"))
from pharos_pricing import calendar as cal  # noqa: E402

# The markets the site already publishes, with the Arc addresses they were
# opened at. A bake keeps these even after the release date has passed so
# the fallback board does not go blank when the calendar window moves on.
PUBLISHED: tuple[tuple[str, str, float, str], ...] = (
    ("CPI_MOM", "May 2026", 3.0, "0x327f5C99eaDf35022c833dBD734A21e8C7C52614"),
    ("GDP", "Q1 2026 (2nd)", 2.0, "0x343d8Da8834c3CFA75540b12bD16946d78390130"),
    ("NFP", "May 2026", 125.0, "0xFb1d30Ca3Ea856174b48338660B01C2b081E5973"),
    ("FOMC_CUT", "Jun 2026", 25.0, "0x01F223cb6B5EA0eB7C4BEb61f42439158ee33ac1"),
)

TIERS: dict[str, tuple[str, str, str]] = {
    "CPI_MOM": (
        "edge",
        "full",
        "OOS-validated: the Inflation Heat pillar beats a naive trend baseline out of sample. Full conviction.",
    ),
    "GDP": (
        "signal",
        "high",
        "Activity Pulse + Capex Thrust, calibrated and out-of-sample checked (IC +0.30, holdout +0.59). Full conviction.",
    ),
    "NFP": (
        "abstain",
        "none",
        "The in-sample fit looks strong but does not survive out of sample. The framework declines to post conviction here.",
    ),
    "FOMC_CUT": (
        "experimental",
        "modeled",
        "A transparent rate reaction function, not a calibrated market. Shown for completeness.",
    ),
}

DEFAULT_FACTORY = "0xfA2D29c4bEd132009D029308810240863c1B3Cc9"
ARCSCAN = "https://testnet.arcscan.app"
SNAPSHOT_PATH = ROOT / "web" / "lib" / "snapshot.ts"


def publication(quote: dict, address: str = "") -> dict:
    """Attach the curated conviction tier and the known on-chain address."""
    tier, conviction, note = TIERS.get(
        quote["kind"],
        ("experimental", "modeled", "Priced by the framework."),
    )
    out = dict(quote)
    out["tier"] = tier
    out["conviction"] = conviction
    out["tier_note"] = note
    out["crowd_prob"] = 0.5
    out["on_chain"] = bool(address)
    out["framework_on_chain"] = bool(address)
    if address:
        out["market_address"] = address
    return out


def apply_chain(markets: list[dict], rows: list[dict]) -> list[dict]:
    """Overlay a live chain read onto baked quotes. Unmatched rows are ignored.

    Demo markets and anything the calendar does not publish stay off the
    static board; the browser adds those itself when it reads the factory.
    """
    by_question = {r["question"]: r for r in rows}
    by_address = {r["address"].lower(): r for r in rows if r.get("address")}
    merged = []
    for market in markets:
        row = by_question.get(market.get("question"))
        addr = str(market.get("market_address") or "")
        if row is None and addr:
            row = by_address.get(addr.lower())
        if row is None:
            merged.append(market)
            continue
        fw = float(row["framework_prob"])
        updated = dict(market)
        updated["crowd_prob"] = float(row["crowd_prob"])
        updated["framework_prob"] = round(fw, 4)
        updated["framework_prob_bps"] = int(round(fw * 10_000))
        updated["on_chain"] = True
        updated["framework_on_chain"] = True
        updated["market_address"] = row["address"]
        updated["resolved"] = bool(row.get("resolved"))
        merged.append(updated)
    return merged


def render_snapshot_module(
    markets: list[dict],
    as_of: str,
    factory: str = DEFAULT_FACTORY,
    arcscan: str = ARCSCAN,
) -> str:
    if not markets:
        raise ValueError("refusing to bake an empty board")
    body = json.dumps(markets, indent=2)
    return (
        'import type { Market } from "@/lib/types";\n'
        "\n"
        "// Framework snapshot. Crowd price, the posted framework price, and\n"
        "// resolution are read from the chain in the browser; this file is the\n"
        "// model writeup and the fallback when that read fails. Refresh with\n"
        "// `make snapshot` (scripts/gen_snapshot.py).\n"
        f'export const SNAPSHOT_AS_OF = "{as_of}";\n'
        f'export const FACTORY = "{factory}";\n'
        f'export const ARCSCAN = "{arcscan}";\n'
        "\n"
        f"export const BOARD: Market[] = {body};\n"
    )


def _factory_address() -> str:
    path = ROOT / "deployments" / "arc.json"
    if not path.exists():
        return DEFAULT_FACTORY
    try:
        return json.loads(path.read_text())["factory"]
    except (OSError, KeyError, json.JSONDecodeError):
        return DEFAULT_FACTORY


def _quotes() -> list[dict]:
    from pharos_pricing.models import price_market

    keys = {(k, r) for k, r, _s, _a in PUBLISHED}
    quotes: list[dict] = []
    for kind, ref, strike, address in PUBLISHED:
        quote = price_market(kind, strike, ref_label=ref)
        quote["market_key"] = f"{kind}:{ref}"
        ev = next((e for e in cal.all_events() if e.kind == kind and e.ref_label == ref), None)
        if ev is not None:
            quote["resolve_dt"] = ev.resolve_dt.isoformat()
        quotes.append(publication(quote, address))

    for ev in cal.upcoming(within_days=60):
        if (ev.kind, ev.ref_label) in keys:
            continue
        mk = cal.MARKET_KINDS[ev.kind]
        quote = price_market(ev.kind, mk.default_strike, ref_label=ev.ref_label)
        quote["market_key"] = ev.market_key
        quote["resolve_dt"] = ev.resolve_dt.isoformat()
        quotes.append(publication(quote))
    return quotes


def _chain_rows(network: str | None) -> list[dict]:
    # The baked file is the Arc board. A localhost node must not paint its
    # prices onto that page.
    if (network or "arc") != "arc":
        return []
    try:
        from .chain import Chain

        chain = Chain(network="arc")
    except Exception:  # noqa: BLE001 — a bake still ships the model writeup
        return []
    rows = []
    for addr in chain.factory().functions.allMarkets().call():
        market = chain.market(addr)
        question = market.functions.question().call()
        crowd, fw, _coll, _ry, _rn, resolved, _winner = market.functions.snapshot().call()
        rows.append(
            {
                "address": addr,
                "question": question,
                "crowd_prob": int(crowd) / 10_000,
                "framework_prob": int(fw) / 10_000,
                "resolved": bool(resolved),
            }
        )
    return rows


def write_snapshot(path: Path | None = None, network: str | None = None) -> Path:
    """Rewrite the snapshot. Raises, without touching the file, if pricing fails."""
    quotes = apply_chain(_quotes(), _chain_rows(network))
    text = render_snapshot_module(quotes, date.today().isoformat(), _factory_address())
    target = path or SNAPSHOT_PATH
    tmp = target.with_suffix(".ts.tmp")
    tmp.write_text(text)
    os.replace(tmp, target)
    return target
