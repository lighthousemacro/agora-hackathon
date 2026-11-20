"""The baked board keeps its writeup and takes live prices when they exist."""
from __future__ import annotations

import unittest

from pharos_agents.snapshot_page import apply_chain, publication, render_snapshot_module


def _quote() -> dict:
    return publication(
        {
            "kind": "CPI_MOM",
            "label": "US Headline CPI — consistent trend",
            "ref_label": "May 2026",
            "question": "US headline CPI trend for May 2026 prints >= 3.00% (3m/6m saar)?",
            "strike": 3.0,
            "unit": "% 3m/6m saar",
            "framework_prob": 0.8318,
            "framework_prob_bps": 8318,
            "reasoning": "framework 83.2% YES.",
            "pillar": {"index": "PCI", "zn_score": 0.02},
            "signal_quality": {"ic": 0.3, "n": 241},
            "information_state": {"as_of": "2026-05-22"},
        },
        address="0x327f5C99eaDf35022c833dBD734A21e8C7C52614",
    )


class SnapshotTests(unittest.TestCase):
    def test_chain_read_replaces_prices_and_keeps_the_writeup(self):
        baked = _quote()
        live = apply_chain(
            [baked],
            [
                {
                    "address": "0x327f5C99eaDf35022c833dBD734A21e8C7C52614",
                    "question": baked["question"],
                    "crowd_prob": 0.61,
                    "framework_prob": 0.80,
                    "resolved": False,
                }
            ],
        )
        self.assertEqual(live[0]["crowd_prob"], 0.61)
        self.assertEqual(live[0]["framework_prob"], 0.8)
        self.assertEqual(live[0]["framework_prob_bps"], 8000)
        self.assertEqual(live[0]["reasoning"], baked["reasoning"])
        self.assertEqual(live[0]["tier"], "edge")
        self.assertTrue(live[0]["on_chain"])

    def test_a_demo_market_on_chain_is_not_added_to_the_bake(self):
        baked = apply_chain(
            [_quote()],
            [
                {
                    "address": "0x00000000000000000000000000000000000000aa",
                    "question": "demo",
                    "crowd_prob": 0.1,
                    "framework_prob": 0.2,
                    "resolved": False,
                }
            ],
        )
        self.assertEqual(len(baked), 1)
        self.assertEqual(baked[0]["crowd_prob"], 0.5)

    def test_render_is_a_module_and_refuses_an_empty_board(self):
        text = render_snapshot_module([_quote()], "2026-11-20", "0xabc")
        self.assertIn('export const SNAPSHOT_AS_OF = "2026-11-20"', text)
        self.assertIn("export const BOARD: Market[]", text)
        self.assertIn("0x327f5C99eaDf35022c833dBD734A21e8C7C52614", text)
        with self.assertRaises(ValueError):
            render_snapshot_module([], "2026-11-20")


if __name__ == "__main__":
    unittest.main()
