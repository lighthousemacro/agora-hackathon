"""Resolution stays on the market's own period."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd

from pharos_agents.oracle import (
    history_through,
    later_gdp_estimate_released,
    realized_value,
)


def _cpi() -> pd.Series:
    # Enough months for a 3/6 trend, plus a later month that must not grade May.
    idx = pd.date_range("2024-01-01", periods=18, freq="MS")
    values = [300 + i for i in range(18)]
    values[-1] = 999  # 2025-06-01, standing in as a later print
    # Rebuild so May 2026 is present and June 2026 is the later print.
    idx = pd.date_range("2025-01-01", periods=18, freq="MS")
    values = [320 + i * 0.4 for i in range(17)] + [999.0]
    return pd.Series(values, index=idx)


class HistoryTests(unittest.TestCase):
    def test_history_stops_at_the_reference_period(self):
        series = _cpi()
        may = pd.Timestamp("2026-05-01")
        cut = history_through(series, may)
        self.assertEqual(cut.index[-1], may)
        self.assertNotIn(pd.Timestamp("2026-06-01"), cut.index)

    def test_a_missing_print_is_not_graded(self):
        series = _cpi().iloc[:-2]  # drops May and June 2026
        with self.assertRaises(ValueError):
            history_through(series, pd.Timestamp("2026-05-01"))

    def test_realized_value_uses_the_reference_period_not_the_latest_print(self):
        series = _cpi()

        def observation_series(series_id: str) -> pd.Series:
            self.assertEqual(series_id, "CPIAUCSL")
            return series

        with patch("pharos_agents.oracle.db.observation_series", observation_series):
            value, asof = realized_value(
                "CPI_MOM",
                datetime(2026, 11, 20, tzinfo=timezone.utc),
                "May 2026",
            )
        self.assertEqual(asof, "2026-05-01")
        self.assertNotEqual(value, 999.0)

    def test_a_later_gdp_estimate_blocks_the_earlier_one(self):
        # Q3 third estimate is 23 Dec 2026, 8:30am ET.
        after = int(datetime(2026, 12, 23, 18, 0, tzinfo=timezone.utc).timestamp())
        before = int(datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp())
        self.assertFalse(later_gdp_estimate_released("Q3 2026 (2nd)", before))
        self.assertTrue(later_gdp_estimate_released("Q3 2026 (2nd)", after))


if __name__ == "__main__":
    unittest.main()
