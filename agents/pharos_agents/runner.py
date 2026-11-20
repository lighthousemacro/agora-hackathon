"""Pharos agent runner / orchestrator.

    python -m pharos_agents.runner status
    python -m pharos_agents.runner create   [--lead 14]
    python -m pharos_agents.runner reprice
    python -m pharos_agents.runner resolve  [--force]
    python -m pharos_agents.runner sync                # create + reprice + resolve + settle
    python -m pharos_agents.runner once                # alias of sync
    python -m pharos_agents.runner watch    [--interval 30]
    python -m pharos_agents.runner demo-market --kind CPI_MOM --ref "May 2026" --resolve-in 5
    python -m pharos_agents.runner trade --market 0x.. --side YES --usdc 1500
"""
from __future__ import annotations

import argparse
import signal
import time
from datetime import datetime

from .chain import Chain
from .market_creator import create_due, create_market, reprice_existing
from .oracle import resolve_due
from .schedule import lead_days, sync_interval, watch_loop
from .sync import run_chain_pass

NO, YES = 0, 1


def cmd_status(chain: Chain, _: argparse.Namespace) -> None:
    factory = chain.factory()
    addrs = factory.functions.allMarkets().call()
    if not addrs:
        print("no markets yet")
        return
    print(f"{len(addrs)} market(s):\n")
    for a in addrs:
        m = chain.market(a)
        q = m.functions.question().call()
        mkt, fw, coll, ry, rn, resolved, winner = m.functions.snapshot().call()
        edge = fw - mkt
        state = (
            f"RESOLVED {'YES' if winner == YES else 'NO'}"
            if resolved
            else "open"
        )
        print(f"• {q}")
        print(
            f"    crowd {mkt/100:5.1f}%  |  framework {fw/100:5.1f}%  |  "
            f"edge {edge/100:+5.1f}pp  |  TVL {coll/1e6:,.0f} USDC  |  {state}\n"
            f"    {a}"
        )


def _lead(a: argparse.Namespace) -> int:
    return a.lead if getattr(a, "lead", None) is not None else lead_days()


def cmd_create(chain: Chain, a: argparse.Namespace) -> None:
    create_due(chain, lead_days=_lead(a))


def cmd_reprice(chain: Chain, _: argparse.Namespace) -> None:
    reprice_existing(chain)


def cmd_resolve(chain: Chain, a: argparse.Namespace) -> None:
    resolve_due(chain, force=a.force)


def cmd_sync(chain: Chain, a: argparse.Namespace) -> None:
    result = run_chain_pass(chain, _lead(a))
    print(result.line())


_STOP = False


def cmd_watch(chain: Chain | None, a: argparse.Namespace) -> None:
    """Keep the book current. The first pass runs before the first sleep."""
    interval = a.interval if a.interval is not None else sync_interval()
    interval = max(1.0, interval)
    lead = _lead(a)
    network = a.network

    def stop() -> bool:
        return _STOP

    def step() -> None:
        # Reconnect every pass so a restarted node is picked up, and so a
        # pass that starts before the chain is up simply retries.
        live = chain if chain is not None and chain.w3.is_connected() else Chain(network=network)
        result = run_chain_pass(live, lead)
        print(result.line(), flush=True)

    print(
        f"watching  every {interval:.0f}s  lead {lead}d  "
        f"(first pass now, then create / reprice / resolve / settle)",
        flush=True,
    )
    watch_loop(step, interval, time.sleep, stop)


def cmd_demo_market(chain: Chain, a: argparse.Namespace) -> None:
    # Schedule against the CHAIN clock, not the wall clock — robust to
    # any local-node block-time drift.
    resolve_dt = datetime.fromtimestamp(chain.block_time() + a.resolve_in)
    create_market(chain, a.kind, a.ref, resolve_dt, strike=a.strike)


def cmd_fastforward(chain: Chain, a: argparse.Namespace) -> None:
    """Advance EVM time deterministically (hardhat/anvil) so a demo
    market reaches its resolve time without a wall-clock wait."""
    chain.w3.provider.make_request("evm_increaseTime", [a.seconds])
    chain.w3.provider.make_request("evm_mine", [])
    print(f"  fast-forwarded chain by {a.seconds}s")


def cmd_trade(chain: Chain, a: argparse.Namespace) -> None:
    m = chain.market(a.market)
    usdc = chain.usdc()
    units = chain.usdc_units(a.usdc)
    side = YES if a.side.upper() == "YES" else NO
    # Demo trader self-funds from the open testnet faucet.
    chain.send(usdc.functions.mint(chain.address, units))
    chain.send(usdc.functions.approve(a.market, units))
    before = m.functions.marketProbBps().call()
    chain.send(m.functions.buy(side, units, 0))
    after = m.functions.marketProbBps().call()
    print(
        f"traded {a.usdc:.0f} USDC {a.side.upper()} on {a.market}\n"
        f"  crowd price {before/100:.1f}% -> {after/100:.1f}%"
    )


def main() -> int:
    p = argparse.ArgumentParser(prog="pharos_agents.runner")
    p.add_argument("--network", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("create")
    sp.add_argument("--lead", type=int, default=None)
    sp.set_defaults(fn=cmd_create)

    sp = sub.add_parser("reprice")
    sp.set_defaults(fn=cmd_reprice)

    sp = sub.add_parser("resolve")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_resolve)

    for name, fn in (("sync", cmd_sync), ("once", cmd_sync)):
        sp = sub.add_parser(name)
        sp.add_argument("--lead", type=int, default=None)
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("watch")
    sp.add_argument("--lead", type=int, default=None)
    sp.add_argument("--interval", type=float, default=None)
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser("demo-market")
    sp.add_argument("--kind", required=True)
    sp.add_argument("--ref", required=True)
    sp.add_argument("--resolve-in", type=int, default=5, dest="resolve_in")
    sp.add_argument("--strike", type=float, default=None)
    sp.set_defaults(fn=cmd_demo_market)

    sp = sub.add_parser("fastforward")
    sp.add_argument("--seconds", type=int, default=120)
    sp.set_defaults(fn=cmd_fastforward)

    sp = sub.add_parser("trade")
    sp.add_argument("--market", required=True)
    sp.add_argument("--side", required=True, choices=["YES", "NO", "yes", "no"])
    sp.add_argument("--usdc", type=float, required=True)
    sp.set_defaults(fn=cmd_trade)

    args = p.parse_args()
    if args.cmd == "watch":
        global _STOP

        def _stop(_signum, _frame) -> None:
            global _STOP
            _STOP = True
            print("stopping", flush=True)

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        # Do not connect first. The loop retries until the chain is up.
        args.fn(None, args)
        return 0
    chain = Chain(network=args.network)
    args.fn(chain, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
