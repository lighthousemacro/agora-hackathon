"""Oracle / resolver agent.

After a release time passes, the oracle computes the realized outcome
from official government data and resolves the market. It uses the SAME
transform the pricing engine used to quote the market, so the question,
the framework price, and the resolution are internally consistent. By
default it reads the data the Lighthouse pipeline has already ingested
from FRED/BLS/BEA (real government data); it never fabricates a print.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pricing"))
from pharos_pricing import calendar as cal  # noqa: E402
from pharos_pricing import db  # noqa: E402
from pharos_pricing.transforms import consistent_trend, diff_trend, point_in_time  # noqa: E402

from .chain import Chain  # noqa: E402
from .schedule import ref_for_question, should_resolve  # noqa: E402

NO, YES = 0, 1

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_ESTIMATE_RANK = {"adv": 0, "advance": 0, "2nd": 1, "second": 1, "3rd": 2, "third": 2}
_QUARTER_MONTH = {1: 1, 2: 4, 3: 7, 4: 10}


def reference_period(kind: str, ref_label: str) -> pd.Timestamp | None:
    """Period-start label the market is about, for series stored that way.

    CPI and payrolls are monthly (May 2026 -> 2026-05-01). GDP is the
    quarter start (Q3 2026 -> 2026-07-01), which is how the BEA rate is
    labelled in the database. Anything else, including a demo market, has
    no period and is graded from the information state at `as_of`.
    """
    quarter = re.match(r"Q([1-4])\s+(\d{4})", ref_label.strip())
    if quarter and kind == "GDP":
        return pd.Timestamp(int(quarter.group(2)), _QUARTER_MONTH[int(quarter.group(1))], 1)
    month = re.match(r"([A-Za-z]{3})\s+(\d{4})", ref_label.strip())
    if month and kind in {"CPI_MOM", "NFP"}:
        mon = _MONTHS.get(month.group(1).lower())
        if mon:
            return pd.Timestamp(int(month.group(2)), mon, 1)
    return None


def history_through(series: pd.Series, period: pd.Timestamp) -> pd.Series:
    """History up to and including `period`. Later periods stay out.

    The database keeps one value per period, the latest revision of that
    period, not the vintage published on release morning. This cut stops a
    later month from grading an earlier market. It cannot undo a revision
    of the period itself, because that vintage is not stored.
    """
    period = pd.Timestamp(period)
    if period not in series.index:
        raise ValueError(f"print {period.date()} is not in the series yet")
    return series.loc[:period]


def meeting_day(ref_label: str) -> pd.Timestamp | None:
    for ev in cal.all_events():
        if ev.kind == "FOMC_CUT" and ev.ref_label == ref_label:
            local = ev.resolve_dt.astimezone(cal.ET)
            return pd.Timestamp(local.date())
    return None


def through_meeting(series: pd.Series, day: pd.Timestamp) -> pd.Series:
    """Daily policy rate through the meeting day. Later meetings stay out."""
    day = pd.Timestamp(day).normalize()
    on_day = series[(series.index >= day) & (series.index < day + pd.Timedelta(days=1))]
    if on_day.empty:
        raise ValueError(f"target rate on {day.date()} is not in the series yet")
    return series.loc[: on_day.index.max()]


def later_gdp_estimate_released(ref_label: str, chain_now: int) -> bool:
    """True when a later estimate of this same quarter is already public.

    Advance, second, and third estimates share one database row. Once the
    next estimate's release time has passed, that row is no longer the
    vintage this market was written on, so the oracle leaves it pending.
    """
    quarter = re.match(r"(Q[1-4]\s+\d{4})", ref_label.strip())
    rank_match = re.search(r"\(([^)]+)\)", ref_label)
    if not quarter or not rank_match:
        return False
    rank = _ESTIMATE_RANK.get(rank_match.group(1).strip().lower())
    if rank is None:
        return False
    key = quarter.group(1)
    for ev in cal.all_events():
        if ev.kind != "GDP":
            continue
        other_q = re.match(r"(Q[1-4]\s+\d{4})", ev.ref_label.strip())
        other_r = re.search(r"\(([^)]+)\)", ev.ref_label)
        if not other_q or not other_r or other_q.group(1) != key:
            continue
        other_rank = _ESTIMATE_RANK.get(other_r.group(1).strip().lower())
        if other_rank is None or other_rank <= rank:
            continue
        if int(cal.as_utc(ev.resolve_dt).timestamp()) <= chain_now:
            return True
    return False


def public_series(series: pd.Series, as_of: datetime, lag_days: int) -> pd.Series:
    """Observations public at `as_of` by a fixed publication lag.

    Used for demo markets, which have no calendar period. A named release
    is graded with `history_through` instead, so a short lag cannot drop
    the print the market is about.
    """
    visible = point_in_time(series, pd.Timestamp(as_of), lag_days)
    if visible.empty:
        raise ValueError(f"nothing public by {pd.Timestamp(as_of).date()}")
    return visible


def _for_release(kind: str, series_id: str, ref_label: str | None, as_of: datetime | None) -> pd.Series:
    raw = db.observation_series(series_id)
    if kind == "FOMC_CUT" and ref_label:
        day = meeting_day(ref_label)
        if day is not None:
            return through_meeting(raw, day)
    period = reference_period(kind, ref_label) if ref_label else None
    if period is not None:
        return history_through(raw, period)
    if as_of is None:
        return raw
    return public_series(raw, as_of, db.pub_lag_days(series_id))


def realized_value(
    kind: str,
    as_of: datetime | None = None,
    ref_label: str | None = None,
) -> tuple[float, str]:
    """Realized metric for one market from ingested government data.

    A named release is graded on its own period, and only after that
    period is in the series. Later periods are excluded. The stored value
    is the latest revision of that period: the database does not keep the
    real-time vintage, so a revision of the period itself is what settles
    if the oracle runs after the agency rewrites it.
    """
    if kind == "CPI_MOM":
        s = consistent_trend(_for_release(kind, "CPIAUCSL", ref_label, as_of), 3, 6)
        if s.empty:
            raise ValueError("could not build a CPI trend for this release")
        return float(s.iloc[-1]), s.index[-1].strftime("%Y-%m-%d")
    if kind == "NFP":
        s = diff_trend(_for_release(kind, "PAYEMS", ref_label, as_of), 3, 6)
        if s.empty:
            raise ValueError("could not build a payroll trend for this release")
        return float(s.iloc[-1]), s.index[-1].strftime("%Y-%m-%d")
    if kind == "GDP":
        s = _for_release(kind, "A191RL1Q225SBEA", ref_label, as_of)
        if len(s) < 2:
            raise ValueError("not enough GDP history for this release")
        val = 0.6 * float(s.iloc[-1]) + 0.4 * float(s.iloc[-2])
        return val, s.index[-1].strftime("%Y-%m-%d")
    if kind == "FOMC_CUT":
        s = _for_release(kind, "DFEDTARU", ref_label, as_of)
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
        question = m.functions.question().call()
        strike = m.functions.strikeScaled().call() / cal.STRIKE_SCALE
        ref = ref_for_question(kind, question, strike)
        if ref != "next" and later_gdp_estimate_released(ref, now):
            print(f"  pending {addr} (a later estimate of {ref} is already out)")
            continue
        try:
            value, asof = realized_value(
                kind,
                datetime.fromtimestamp(resolve_time, timezone.utc),
                ref if ref != "next" else None,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  pending {addr}: {e}")
            continue

        outcome = YES if value >= strike else NO
        try:
            chain.send(m.functions.resolve(outcome))
        except Exception as e:  # noqa: BLE001
            print(f"  skip {addr}: {e}")
            continue
        print(
            f"  resolved {kind:<9} {addr}\n"
            f"           realized {value:.2f} vs strike {strike:.2f} "
            f"(data asof {asof}) -> {'YES' if outcome else 'NO'}"
        )
        n += 1
    if n == 0:
        print("  no markets resolved this pass")
    return n
