"""When the agents act.

The calendar, the chain clock, and the posted framework price are three
clocks. This module decides — with no RPC and no database — which markets
to open, which framework prices have actually moved, which markets are
ready to resolve, and which resolved pools the creator can settle. The
runner's sync pass and its watch loop both call these decisions so a
market is opened, repriced, resolved, and settled on the first pass that
is allowed to do it.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pricing"))
from pharos_pricing import calendar as cal  # noqa: E402


def _env_number(name: str, default: float, cast):
    """Read a numeric setting. A bad value is reported and does not stop the pass."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return cast(default)
    try:
        return cast(str(raw).strip())
    except ValueError:
        print(f"{name}={raw!r} is not a number; using {default}", flush=True)
        return cast(default)


def lead_days() -> int:
    """How far ahead of a release a market should already be open."""
    return _env_number("PHAROS_LEAD_DAYS", 14, int)


def sync_interval() -> float:
    """Seconds between watch passes. The first pass does not wait."""
    return _env_number("PHAROS_SYNC_INTERVAL", 30, float)


def reprice_threshold() -> int:
    """Minimum framework move, in basis points, that is worth a chain write."""
    return max(0, _env_number("PHAROS_REPRICE_BPS", 25, int))


def ref_for_question(kind: str, question: str, strike: float) -> str:
    """Calendar reference label for an on-chain market.

    The framework price has to be the price of *this* print. Matching the
    question the contract was created with is what keeps a May CPI market
    from being overwritten with the next month's quote. Unknown markets
    fall back to ``next``, which is what a one-off demo market is.
    """
    mk = cal.MARKET_KINDS.get(kind)
    if mk is None:
        return "next"
    for ev in cal.all_events():
        if ev.kind != kind:
            continue
        expected = mk.question_tmpl.format(ref=ev.ref_label, strike=strike)
        if expected == question:
            return ev.ref_label
    for ev in cal.all_events():
        if ev.kind == kind and ev.ref_label in question:
            return ev.ref_label
    return "next"


def select_creates(
    now: datetime,
    chain_now: int,
    lead: int,
    events: list[cal.ReleaseEvent],
) -> list[cal.ReleaseEvent]:
    """Releases inside the lead window that the chain will still accept.

    A market whose resolve time is already behind the chain clock reverts
    in the constructor, so it is not due — it is late, and the resolver
    owns it. ``lead`` is in days, matching the calendar the creator posts.
    """
    due: list[cal.ReleaseEvent] = []
    now_utc = cal.as_utc(now)
    for ev in events:
        release = cal.as_utc(ev.resolve_dt)
        if int(release.timestamp()) <= chain_now:
            continue
        days_out = (release - now_utc).days
        if 0 <= days_out <= lead:
            due.append(ev)
    return due


def should_reprice(old_bps: int, new_bps: int, threshold: int | None = None) -> bool:
    """True when the framework quote moved enough to rewrite on-chain."""
    band = reprice_threshold() if threshold is None else threshold
    return abs(int(new_bps) - int(old_bps)) >= band


def should_resolve(chain_now: int, resolve_time: int, resolved: bool, force: bool = False) -> bool:
    """True once chain time has reached the release. The contract uses the same clock."""
    if resolved:
        return False
    return bool(force) or chain_now >= resolve_time


def should_settle(resolved: bool, reserves: int, caller_is_lp: bool) -> bool:
    """True when the creator can take its residual back. Idempotent: empty pools are skipped."""
    return bool(resolved) and bool(caller_is_lp) and int(reserves) > 0


@dataclass(frozen=True)
class PassResult:
    created: int
    repriced: int
    held: int
    resolved: int
    settled: int
    seconds: float

    def line(self) -> str:
        return (
            f"sync  created={self.created} repriced={self.repriced} "
            f"held={self.held} resolved={self.resolved} settled={self.settled}  "
            f"{self.seconds:.1f}s"
        )


def run_pass(chain, lead: int, *, create, reprice, resolve, settle) -> PassResult:
    """One full pass, in the order a release actually happens.

    Open anything that entered the lead window, post framework prices that
    moved, resolve releases the chain clock has reached, then return leftover
    liquidity. Each callback owns its own per-market errors; a raise here is
    a pass-level failure (no chain, no deployment) and the watch loop retries.
    """
    started = time.perf_counter()
    created = int(create(chain, lead))
    repriced, held = reprice(chain)
    resolved = int(resolve(chain))
    settled = int(settle(chain))
    return PassResult(
        created=created,
        repriced=int(repriced),
        held=int(held),
        resolved=resolved,
        settled=settled,
        seconds=time.perf_counter() - started,
    )


def watch_loop(step, interval: float, sleep, stop, max_passes: int | None = None) -> int:
    """Run ``step`` immediately, then again every ``interval`` seconds.

    A failing pass is reported and does not stop the loop: the next pass is
    the retry. ``stop`` is polled during the wait so a signal ends the
    process without waiting out the interval.
    """
    passes = 0
    while not stop():
        try:
            step()
        except Exception as exc:  # noqa: BLE001 — one bad pass must not kill the loop
            print(f"sync pass failed: {exc}", flush=True)
        passes += 1
        if max_passes is not None and passes >= max_passes:
            return passes
        waited = 0.0
        while waited < interval and not stop():
            slice_s = min(0.25, interval - waited)
            sleep(slice_s)
            waited += slice_s
    return passes
