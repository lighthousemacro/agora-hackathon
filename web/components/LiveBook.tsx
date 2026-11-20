"use client";

import { useEffect, useState } from "react";
import FlagshipSpread from "@/components/FlagshipSpread";
import MarketCard from "@/components/MarketCard";
import { fetchOnchainMarkets } from "@/lib/chain";
import { CHAIN_POLL_MS, mergeBoard } from "@/lib/merge";
import type { Market } from "@/lib/types";

type SyncState = "reading" | "live" | "waiting" | "stale";

function ageLabel(syncedAt: number | null, now: number): string {
  if (syncedAt == null) return "";
  const seconds = Math.max(0, Math.round((now - syncedAt) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 90) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  return `${minutes}m ago`;
}

export default function LiveBook({
  initial,
  asOf,
  factory,
  arcscan,
}: {
  initial: Market[];
  asOf: string;
  factory: string;
  arcscan: string;
}) {
  const [markets, setMarkets] = useState(initial);
  const [state, setState] = useState<SyncState>("reading");
  const [syncedAt, setSyncedAt] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    let stop = false;
    let inflight = false;

    const pull = async () => {
      if (inflight) return;
      inflight = true;
      try {
        const rows = await fetchOnchainMarkets();
        if (stop) return;
        setMarkets(mergeBoard(initial, rows));
        setSyncedAt(Date.now());
        setState("live");
      } catch {
        if (stop) return;
        setState((current) => (current === "live" || current === "stale" ? "stale" : "waiting"));
      } finally {
        inflight = false;
      }
    };

    pull();
    const poll = setInterval(pull, CHAIN_POLL_MS);
    const clock = setInterval(() => setNow(Date.now()), 1000);
    const onVisible = () => {
      if (document.visibilityState === "visible") pull();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stop = true;
      clearInterval(poll);
      clearInterval(clock);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [initial]);

  const flagship = markets.find((m) => m.tier === "edge") ?? markets[0];
  const board = markets.filter((m) => m !== flagship);
  const age = ageLabel(syncedAt, now);
  const banner =
    state === "live"
      ? `Chain synced ${age}`
      : state === "stale"
        ? `Last chain sync ${age}`
        : state === "waiting"
          ? "Waiting on the chain"
          : "Reading the chain";

  return (
    <>
      {flagship && (
        <section className="band">
          <div className="kicker">The spread, live</div>
          <FlagshipSpread m={flagship} />
        </section>
      )}

      <section className="band">
        <div className="sec-head">
          <div>
            <div className="kicker">The board</div>
            <h2>Graded by how much it trusts itself</h2>
          </div>
          <p className="note">
            We only claim conviction where the model beats a naive baseline out
            of sample. Everywhere else it says so, on the card.
          </p>
        </div>
        <div className="grid">
          {board.map((m, i) => (
            <MarketCard key={m.market_address ?? `${m.kind}-${i}`} m={m} />
          ))}
        </div>
        <div style={{ marginTop: 16 }}>
          <span
            className={`banner${state === "live" ? "" : " sample"}`}
            data-sync={state}
            aria-live="polite"
          >
            <span className="dot" />
            {banner}
            {" · "}
            framework snapshot <b>{asOf}</b>
            {" · "}
            {markets.length} market{markets.length === 1 ? "" : "s"} on{" "}
            <a href={`${arcscan}/address/${factory}`} target="_blank" rel="noopener noreferrer">
              Arc testnet
            </a>
          </span>
        </div>
      </section>
    </>
  );
}
