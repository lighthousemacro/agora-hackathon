"""Market-creator agent.

Reads the economic-release calendar, and for every release inside the
lead window opens a binary market seeded with USDC liquidity, posting the
Lighthouse framework's point-in-time fair value on-chain at creation. It
can also re-post the framework price on existing markets as new data
lands — the framework price is a *live* oracle, not a one-time seed.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from web3 import Web3
from web3.logs import DISCARD

# Use the pricing engine in-process (self-contained; no running service
# required for the demo).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pricing"))
from pharos_pricing import calendar as cal  # noqa: E402
from pharos_pricing.models import price_market  # noqa: E402

from .chain import Chain  # noqa: E402
from .schedule import (  # noqa: E402
    ref_for_question,
    reprice_threshold,
    select_creates,
    should_reprice,
)


def _key(market_key: str) -> bytes:
    return Web3.keccak(text=market_key)


def _seed_units(chain: Chain) -> int:
    return chain.usdc_units(float(os.environ.get("PHAROS_SEED_USDC", "5000")))


def create_market(
    chain: Chain,
    kind: str,
    ref_label: str,
    resolve_dt: datetime,
    strike: float | None = None,
    seed_usdc: int | None = None,
) -> tuple[str, bool]:
    """Open one market. Returns ``(address, created)``.

    ``created`` is false when the calendar key is already on the factory,
    so a sync pass can count real opens and leave the existing market alone.
    """
    factory = chain.factory()
    market_key = f"{kind}:{ref_label}"
    existing = factory.functions.marketByKey(_key(market_key)).call()
    if int(existing, 16) != 0:
        print(f"  exists  {market_key} -> {existing}")
        return existing, False

    quote = price_market(kind, strike, ref_label=ref_label)
    strike_scaled = quote["strike_scaled"]
    fw_bps = quote["framework_prob_bps"]
    resolve_ts = int(resolve_dt.timestamp())
    seed = seed_usdc if seed_usdc is not None else _seed_units(chain)

    # Approve the factory to pull the seed, then create + seed atomically.
    usdc = chain.usdc()
    chain.send(usdc.functions.approve(factory.address, seed))
    rcpt = chain.send(
        factory.functions.createMarket(
            _key(market_key),
            quote["question"],
            strike_scaled,
            resolve_ts,
            kind,
            fw_bps,
            seed,
        )
    )
    ev = factory.events.MarketCreated().process_receipt(rcpt, errors=DISCARD)[0]
    addr = ev["args"]["market"]
    print(
        f"  created {market_key:<22} -> {addr}\n"
        f"          framework {fw_bps/100:.1f}% | seed {seed/1e6:.0f} USDC | "
        f"resolves {resolve_dt.isoformat()}\n"
        f"          {quote['reasoning']}"
    )
    return addr, True


def create_due(chain: Chain, lead_days: int = 14, within_days: int | None = None) -> int:
    """Open every release inside the lead window. Returns how many were new.

    The chain clock gates creation: a release the chain has already reached
    would revert in the constructor, and one failure must not stop the rest
    of the pass.
    """
    window = within_days if within_days is not None else max(lead_days, 60)
    now = datetime.now(timezone.utc)
    due = select_creates(now, chain.block_time(), lead_days, cal.upcoming(within_days=window))
    created = 0
    for ev in due:
        try:
            _addr, is_new = create_market(chain, ev.kind, ev.ref_label, ev.resolve_dt)
        except Exception as e:  # noqa: BLE001
            print(f"  skip {ev.market_key}: {e}")
            continue
        if is_new:
            created += 1
    if created == 0 and not due:
        print("  nothing due inside the lead window")
    return created


def reprice_existing(chain: Chain) -> tuple[int, int]:
    """Re-post the framework probability on every open market.

    The quote is for the market's own reference print, not whatever the
    calendar currently calls "next". Returns ``(rewritten, held)``.
    """
    threshold = reprice_threshold()
    factory = chain.factory()
    rewritten = held = 0
    for addr in factory.functions.allMarkets().call():
        m = chain.market(addr)
        mkt_bps, fw_bps, _, _, _, resolved, _ = m.functions.snapshot().call()
        if resolved:
            continue
        kind = m.functions.resolverKind().call()
        strike = m.functions.strikeScaled().call() / cal.STRIKE_SCALE
        question = m.functions.question().call()
        ref = ref_for_question(kind, question, strike)
        try:
            quote = price_market(kind, strike, ref_label=ref)
        except Exception as e:  # noqa: BLE001
            print(f"  skip {addr}: {e}")
            continue
        new_bps = quote["framework_prob_bps"]
        if should_reprice(fw_bps, new_bps, threshold):
            try:
                chain.send(m.functions.updateFrameworkProb(new_bps))
            except Exception as e:  # noqa: BLE001
                print(f"  skip {addr}: {e}")
                continue
            rewritten += 1
            print(
                f"  repriced {kind:<9} {ref:<16} "
                f"{fw_bps/100:.1f}% -> {new_bps/100:.1f}%"
            )
        else:
            held += 1
            print(f"  hold     {kind:<9} {ref:<16} {fw_bps/100:.1f}% (mkt {mkt_bps/100:.1f}%)")
    return rewritten, held
