"""A late resolve still uses the print that was public at the release."""
from __future__ import annotations

import unittest
from datetime import datetime

import pandas as pd

from pharos_agents.oracle import public_series


class PublicSeriesTests(unittest.TestCase):
    def test_a_later_print_does_not_enter_a_catch_up_resolve(self):
        # April CPI is labelled 2026-04-01 and is public ~41 days later.
        series = pd.Series(
            [100.0, 200.0],
            index=pd.to_datetime(["2026-04-01", "2026-05-01"]),
        )
        at_release = public_series(series, datetime(2026, 5, 12), lag_days=41)
        self.assertEqual(list(at_release.values), [100.0])
        # Graded later, the May print is public. A catch-up resolve passes the
        # original release time, so it still sees only the April print.
        later = public_series(series, datetime(2026, 6, 20), lag_days=41)
        self.assertEqual(list(later.values), [100.0, 200.0])


if __name__ == "__main__":
    unittest.main()
