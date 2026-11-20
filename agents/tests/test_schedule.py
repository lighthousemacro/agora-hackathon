"""Decisions the sync pass makes before it touches a chain."""
from __future__ import annotations

import unittest
from datetime import datetime

from pharos_pricing import calendar as cal

from pharos_agents.schedule import (
    PassResult,
    ref_for_question,
    run_pass,
    select_creates,
    should_reprice,
    should_resolve,
    should_settle,
    watch_loop,
)


def _event(kind: str, ref: str) -> cal.ReleaseEvent:
    for ev in cal.all_events():
        if ev.kind == kind and ev.ref_label == ref:
            return ev
    raise AssertionError(f"no fixture event {kind}:{ref}")


class RefTests(unittest.TestCase):
    def test_matches_the_print_the_market_was_opened_for(self):
        mk = cal.MARKET_KINDS["CPI_MOM"]
        question = mk.question_tmpl.format(ref="May 2026", strike=3.0)
        self.assertEqual(ref_for_question("CPI_MOM", question, 3.0), "May 2026")

    def test_does_not_confuse_a_later_print_of_the_same_kind(self):
        mk = cal.MARKET_KINDS["CPI_MOM"]
        question = mk.question_tmpl.format(ref="Jun 2026", strike=3.0)
        self.assertEqual(ref_for_question("CPI_MOM", question, 3.0), "Jun 2026")
        self.assertNotEqual(ref_for_question("CPI_MOM", question, 3.0), "May 2026")

    def test_demo_markets_fall_back_to_next(self):
        self.assertEqual(ref_for_question("CPI_MOM", "DEMO question", 3.0), "next")

    def test_nfp_strike_format_matches_the_template(self):
        mk = cal.MARKET_KINDS["NFP"]
        question = mk.question_tmpl.format(ref="May 2026", strike=125.0)
        self.assertEqual(ref_for_question("NFP", question, 125.0), "May 2026")


class CreateWindowTests(unittest.TestCase):
    def test_opens_inside_the_lead_and_skips_what_the_chain_would_reject(self):
        may = _event("CPI_MOM", "May 2026")
        jun = _event("CPI_MOM", "Jun 2026")
        # 1 Jun is 9 days before the 10 Jun May-CPI release, and well before July's.
        now = datetime(2026, 6, 1, 12, 0)
        chain_now = int(now.timestamp())
        due = select_creates(now, chain_now, lead=14, events=[may, jun])
        self.assertEqual([e.ref_label for e in due], ["May 2026"])

    def test_a_release_the_chain_has_already_reached_is_not_opened(self):
        may = _event("CPI_MOM", "May 2026")
        # Wall clock still thinks the release is ahead; the chain does not.
        now = datetime(2026, 6, 1, 12, 0)
        chain_now = int(may.resolve_dt.timestamp())
        due = select_creates(now, chain_now, lead=14, events=[may])
        self.assertEqual(due, [])

    def test_on_20_nov_2026_the_lead_window_is_payrolls_and_the_gdp_second_estimate(self):
        now = datetime(2026, 11, 20, 12, 0)
        due = select_creates(now, int(now.timestamp()), lead=14, events=cal.all_events())
        self.assertEqual(
            {e.market_key for e in due},
            {"GDP:Q3 2026 (2nd)", "NFP:Nov 2026"},
        )

    def test_outside_the_lead_window_waits(self):
        may = _event("CPI_MOM", "May 2026")
        now = datetime(2026, 5, 1, 0, 0)
        due = select_creates(now, int(now.timestamp()), lead=14, events=[may])
        self.assertEqual(due, [])


class ActionTests(unittest.TestCase):
    def test_reprice_only_on_a_real_move(self):
        self.assertFalse(should_reprice(5000, 5024, threshold=25))
        self.assertTrue(should_reprice(5000, 5025, threshold=25))
        self.assertTrue(should_reprice(5000, 4900, threshold=25))

    def test_resolve_uses_the_chain_clock(self):
        self.assertFalse(should_resolve(100, 101, resolved=False))
        self.assertTrue(should_resolve(101, 101, resolved=False))
        self.assertFalse(should_resolve(200, 101, resolved=True))
        self.assertTrue(should_resolve(50, 101, resolved=False, force=True))

    def test_settle_is_the_lp_residual_and_only_once(self):
        self.assertTrue(should_settle(resolved=True, reserves=1, caller_is_lp=True))
        self.assertFalse(should_settle(resolved=True, reserves=0, caller_is_lp=True))
        self.assertFalse(should_settle(resolved=False, reserves=10, caller_is_lp=True))
        self.assertFalse(should_settle(resolved=True, reserves=10, caller_is_lp=False))


class PassTests(unittest.TestCase):
    def test_one_pass_runs_create_then_reprice_then_resolve_then_settle(self):
        order: list[str] = []

        def create(chain, lead):
            order.append(f"create:{lead}")
            return 2

        def reprice(chain):
            order.append("reprice")
            return 1, 3

        def resolve(chain):
            order.append("resolve")
            return 1

        def settle(chain):
            order.append("settle")
            return 1

        result = run_pass(
            object(),
            14,
            create=create,
            reprice=reprice,
            resolve=resolve,
            settle=settle,
        )
        self.assertEqual(order, ["create:14", "reprice", "resolve", "settle"])
        self.assertEqual(result.created, 2)
        self.assertEqual(result.repriced, 1)
        self.assertEqual(result.held, 3)
        self.assertEqual(result.resolved, 1)
        self.assertEqual(result.settled, 1)
        self.assertIn("created=2", result.line())
        self.assertIn("settled=1", result.line())

    def test_watch_runs_the_first_pass_immediately_and_survives_a_failure(self):
        calls = {"n": 0}

        def step():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("rpc down")

        slept: list[float] = []
        passes = watch_loop(
            step,
            interval=0,
            sleep=slept.append,
            stop=lambda: False,
            max_passes=2,
        )
        self.assertEqual(passes, 2)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(slept, [])

    def test_watch_stops_during_the_wait(self):
        calls = {"n": 0, "asleep": False}

        def step():
            calls["n"] += 1

        def stop():
            return calls["asleep"]

        def sleep(_seconds):
            calls["asleep"] = True

        passes = watch_loop(step, interval=30, sleep=sleep, stop=stop)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(passes, 1)


class SummaryTests(unittest.TestCase):
    def test_summary_line_names_every_count(self):
        line = PassResult(0, 1, 4, 0, 0, 0.84).line()
        self.assertEqual(
            line,
            "sync  created=0 repriced=1 held=4 resolved=0 settled=0  0.8s",
        )


if __name__ == "__main__":
    unittest.main()
