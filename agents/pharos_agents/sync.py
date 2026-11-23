"""One autonomous pass over the book, and the loop that keeps doing it.

Order is the life of a market: open it once it is inside the lead window,
repost the framework price when that print's quote moves, resolve it when
the chain clock reaches the release, then return the creator's residual
liquidity. ``watch`` runs that pass immediately and then forever.
"""
from __future__ import annotations

import os

from web3 import Web3

from .chain import Chain
from .market_creator import create_due, reprice_existing
from .oracle import resolve_due
from .schedule import PassResult, run_pass, should_settle


def settle_resolved(chain: Chain) -> int:
    """Withdraw the creator's residual on resolved markets. Already-empty pools are left alone."""
    factory = chain.factory()
    n = 0
    seen = False
    for addr in factory.functions.allMarkets().call():
        m = chain.market(addr)
        _mkt, _fw, _coll, r_yes, r_no, resolved, _winner = m.functions.snapshot().call()
        if not resolved:
            continue
        seen = True
        try:
            lp = m.functions.lp().call()
            caller_is_lp = Web3.to_checksum_address(lp) == chain.address
            reserves = int(r_yes) + int(r_no)
            if not should_settle(True, reserves, caller_is_lp):
                continue
            chain.send(m.functions.withdrawLiquidity())
        except Exception as e:  # noqa: BLE001
            print(f"  skip settle {addr}: {e}")
            continue
        print(f"  settled  {addr}  residual {reserves / 1e6:,.0f} USDC")
        n += 1
    if n == 0:
        print("  no liquidity to settle" if seen else "  no resolved markets")
    return n


def _write_snapshot() -> None:
    """Refresh the static board when a pass asked for it. Never fails the pass."""
    if os.environ.get("PHAROS_WRITE_SNAPSHOT", "").strip() not in {"1", "true", "yes"}:
        return
    try:
        from .snapshot_page import write_snapshot

        path = write_snapshot()
        print(f"  snapshot {path}")
    except Exception as e:  # noqa: BLE001
        print(f"  snapshot skipped: {e}")


def run_chain_pass(chain: Chain, lead_days: int) -> PassResult:
    """Create, reprice, resolve, settle. Banners print in that order."""

    def create(c: Chain, lead: int) -> int:
        print("[create]")
        return create_due(c, lead_days=lead)

    def reprice(c: Chain) -> tuple[int, int]:
        print("[reprice]")
        return reprice_existing(c)

    def resolve(c: Chain) -> int:
        print("[resolve]")
        return resolve_due(c, force=False)

    def settle(c: Chain) -> int:
        print("[settle]")
        return settle_resolved(c)

    result = run_pass(
        chain,
        lead_days,
        create=create,
        reprice=reprice,
        resolve=resolve,
        settle=settle,
    )
    _write_snapshot()
    return result
