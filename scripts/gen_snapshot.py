#!/usr/bin/env python3
"""Rewrite web/lib/snapshot.ts from the pricing engine and, if reachable, the chain.

    make snapshot

Leaves the current file untouched when the database cannot price the board.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT / "pricing")]

from pharos_agents.snapshot_page import write_snapshot  # noqa: E402


def main() -> int:
    try:
        path = write_snapshot()
    except Exception as exc:  # noqa: BLE001
        print(f"snapshot left unchanged: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
