import type { OnchainMarket } from "@/lib/chain";
import { normQuestion } from "@/lib/chain";
import type { Market } from "@/lib/types";

/** How often the board re-reads the factory. The first read does not wait. */
export const CHAIN_POLL_MS = 10_000;

/** Release time has passed and the oracle has not resolved the market yet. */
export function releasePassed(market: Market, now = Date.now()): boolean {
  if (market.resolved) return false;
  const ts = market.resolve_time
    ? market.resolve_time * 1000
    : market.resolve_dt
      ? Date.parse(market.resolve_dt)
      : Number.NaN;
  return Number.isFinite(ts) && ts <= now;
}

function apply(market: Market, row: OnchainMarket): Market {
  return {
    ...market,
    kind: row.kind || market.kind,
    crowd_prob: row.crowdProb,
    framework_prob: row.frameworkProb,
    framework_prob_bps: Math.round(row.frameworkProb * 10_000),
    on_chain: true,
    framework_on_chain: true,
    market_address: row.address,
    resolved: row.resolved,
    resolve_time: row.resolveTime,
  };
}

function fromChain(row: OnchainMarket): Market {
  const when = row.resolveTime
    ? new Date(row.resolveTime * 1000).toISOString().slice(0, 16)
    : undefined;
  return {
    kind: row.kind || "ONCHAIN",
    label: row.question,
    ref_label: "",
    question: row.question,
    strike: 0,
    unit: "",
    framework_prob: row.frameworkProb,
    framework_prob_bps: Math.round(row.frameworkProb * 10_000),
    nowcast: 0,
    sigma: 0,
    pillar: { index: "", zn_score: 0 },
    signal_quality: {
      beta: 0,
      resid_std: 0,
      ic: 0,
      bal_accuracy: 0.5,
      hit_rate: 0.5,
      n: 0,
      shrink: 0,
    },
    information_state: {
      as_of: "",
      eop_lag_days: 0,
      pub_lag_days: 0,
      grading: 0,
      inputs: {},
    },
    reasoning: "",
    resolve_dt: when,
    tier: "experimental",
    conviction: "modeled",
    tier_note: "Live on Arc. The model writeup catches up on the next snapshot.",
    crowd_prob: row.crowdProb,
    on_chain: true,
    framework_on_chain: true,
    market_address: row.address,
    resolved: row.resolved,
    resolve_time: row.resolveTime,
  };
}

/**
 * Join the baked model writeup to a factory read.
 * Prices and resolution come from the chain. A market the factory no longer
 * lists drops its on-chain flag. A market the snapshot has never seen is
 * appended so a new open shows up without a rebuild.
 */
export function mergeBoard(board: Market[], chain: OnchainMarket[]): Market[] {
  const byQuestion = new Map<string, OnchainMarket>();
  const byAddress = new Map<string, OnchainMarket>();
  for (const row of chain) {
    byQuestion.set(normQuestion(row.question), row);
    byAddress.set(row.address.toLowerCase(), row);
  }
  const used = new Set<string>();
  const merged = board.map((market) => {
    const hit =
      (market.market_address && byAddress.get(market.market_address.toLowerCase())) ||
      byQuestion.get(normQuestion(market.question));
    if (!hit) return { ...market, on_chain: false, framework_on_chain: false };
    used.add(hit.address.toLowerCase());
    return apply(market, hit);
  });
  for (const row of chain) {
    if (!used.has(row.address.toLowerCase())) merged.push(fromChain(row));
  }
  return merged;
}
