"""Oracle / resolver agent.

After a release time passes, the oracle computes the realized outcome
from official government data and resolves the market. It uses the SAME
transform the pricing engine used to quote the market, so the question,
the framework price, and the resolution are internally consistent. By
default it reads the data the Lighthouse pipeline has already ingested
from FRED/BLS/BEA (real government data); it never fabricates a print.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pricing"))
from pharos_pricing import calendar as cal  # noqa: E402
from pharos_pricing import db  # noqa: E402
from pharos_pricing.transforms import consistent_trend, diff_trend, point_in_time  # noqa: E402

from .chain import Chain  # noqa: E402
from .schedule import should_resolve  # noqa: E402

NO, YES = 0, 1


def public_series(series: pd.Series, as_of: datetime, lag_days: int) -> pd.Series:
    """Observations that were already public at `as_of`.

    A resolve that runs late still grades the release it was written for.
    Later prints stay out of the outcome.
    """
    visible = point_in_time(series, pd.Timestamp(as_of), lag_days)
    if visible.empty:
        raise ValueError(f"nothing public by {as_of.date()}")
    return visible


def _series(series_id: str, as_of: datetime | None) -> pd.Series:
    raw = db.observation_series(series_id)
    if as_of is None:
        return raw
    return public_series(raw, as_of, db.pub_lag_days(series_id))


def realized_value(kind: str, as_of: datetime | None = None) -> tuple[float, str]:
    """Realized metric for `kind` from ingested government data.

    ``as_of`` is the market's release time. Passing it keeps a catch-up
    resolve on the print that was public then.
    """
    if kind == "CPI_MOM":
        s = consistent_trend(_series("CPIAUCSL", as_of), 3, 6)
        return float(s.iloc[-1]), s.index[-1].strftime("%Y-%m-%d")
    if kind == "NFP":
        s = diff_trend(_series("PAYEMS", as_of), 3, 6)
        return float(s.iloc[-1]), s.index[-1].strftime("%Y-%m-%d")
    if kind == "GDP":
        s = _series("A191RL1Q225SBEA", as_of)
        val = 0.6 * float(s.iloc[-1]) + 0.4 * float(s.iloc[-2])
        return val, s.index[-1].strftime("%Y-%m-%d")
    if kind == "FOMC_CUT":
        s = _series("DFEDTARU", as_of)
        now_rate = float(s.iloc[-1])
        prior = float(s.iloc[-22]) if len(s) > 22 else float(s.iloc[0])
        cut_bps = (prior - now_rate) * 100.0
        return cut_bps, s.index[-1].strftime("%Y-%m-%d")
    raise ValueError(f"no resolver for {kind}")


def resolve_due(chain: Chain, force: bool = False) -> int:
    factory = chain.factory()
    n = 0
    # Gate on CHAIN time — the contract enforces resolution against
    # block.timestamp, so the oracle must use the same clock.
    now = chain.block_time()
    for addr in factory.functions.allMarkets().call():
        m = chain.market(addr)
        _, _, _, _, _, resolved, _ = m.functions.snapshot().call()
        resolve_time = m.functions.resolveTime().call()
        if not should_resolve(now, resolve_time, resolved, force):
            if not resolved:
                remaining = resolve_time - now
                print(f"  pending {addr} ({remaining}s to resolve time)")
            continue

        kind = m.functions.resolverKind().call()
        strike = m.functions.strikeScaled().call() / cal.STRIKE_SCALE
        try:
            value, asof = realized_value(kind, datetime.fromtimestamp(resolve_time))
        except Exception as e:  # noqa: BLE001
            print(f"  skip {addr}: {e}")
            continue

        outcome = YES if value >= strike else NO
        chain.send(m.functions.resolve(outcome))
        print(
            f"  resolved {kind:<9} {addr}\n"
            f"           realized {value:.2f} vs strike {strike:.2f} "
            f"(data asof {asof}) -> {'YES' if outcome else 'NO'}"
        )
        n += 1
    if n == 0:
        print("  no markets resolved this pass")
    return n
