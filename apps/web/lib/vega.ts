/* Vega command parser: pure, deterministic, unit-tested (tests/vega.test.mjs).

   Safety rules
   * Operational (state-changing) commands - kill switch, replay, research - must match an exact command
     phrase. Anything that only mentions them becomes a clarifying question, never an action.
   * A negation ("don't stop trading") or a question ("what is the kill switch?") never triggers an action.
   * Low speech-recognition confidence never triggers an operational action.
   * Every operational intent needs an explicit confirmation before it runs (the page enforces this).
   * Resetting or disabling any emergency safety mechanism is refused, always.
   * Order placement is refused: real orders do not exist in this lab. */

export type Intent =
  | { kind: "status" } | { kind: "news" } | { kind: "portfolio" }
  | { kind: "open"; path: string; name: string }
  | { kind: "replay" } | { kind: "research" } | { kind: "kill" }
  | { kind: "confirm" } | { kind: "cancel" }
  | { kind: "refuse_order" } | { kind: "refuse_reset" }
  | { kind: "clarify"; reason: string }
  | { kind: "ask"; text: string }
  | { kind: "empty" };

export const OPERATIONAL = new Set(["replay", "research", "kill"]);
export const MIN_CONFIDENCE = 0.6;
export const CONFIRM_WINDOW_MS = 10_000;

export const PAGES: [RegExp, string, string][] = [
  [/^(the )?strategy monitor$|^screening$/, "/screening", "Strategy Monitor"],
  [/^(the )?learning brain$|^learning$/, "/learning", "Learning Brain"],
  [/^(the )?backtest(ing)?( studio)?$/, "/backtest", "Backtesting Studio"],
  [/^(the )?risk( control)?( center| centre| page)?$/, "/risk", "Risk Control Center"],
  [/^(the )?paper( trading)?( terminal)?$/, "/paper", "Paper Trading Terminal"],
  [/^(the )?command center$/, "/command", "Command Center"],
  [/^(the )?mission control$|^home$|^overview$/, "/", "Mission Control"],
];

export function normalize(raw: string): string {
  return raw.toLowerCase().replace(/[’']/g, "'").replace(/[^a-z0-9' ]+/g, " ").replace(/\s+/g, " ").trim()
    .replace(/^(hey |ok |okay )?vega,? /, "").replace(/^please /, "").replace(/ please$/, "").replace(/ now$/, "");
}

const NEGATION = /\b(don't|dont|do not|not|never|no|without|shouldn't|wouldn't|stop me)\b/;
const QUESTION = /^(what|why|how|when|who|which|is|are|was|does|do|did|can|could|should|would|will|explain|tell me about|describe)\b/;
const OPERATIONAL_WORDS = /\b(kill|switch|halt|stop|emergency|replay|research|trading)\b/;

const KILL = [/^(engage|activate|trigger|hit|press|pull)( the)? (kill switch|emergency stop)$/, /^(stop|halt) (all )?(trading|everything)$/,
  /^emergency stop$/, /^kill switch on$/];
const REPLAY = [/^(run|start)( a)?( new)?( (5|five) session)? replay$/];
const RESEARCH = [/^(run|start)( a| the| new)? research( run)?$/];

export function parse(raw: string, confidence?: number): Intent {
  const s = normalize(raw);
  if (!s) return { kind: "empty" };
  if (/^(confirm|confirmed|yes confirm|confirm it|yes do it|do it)$/.test(s)) return { kind: "confirm" };
  if (/^(cancel|no|stop|never mind|nevermind|abort|forget it)$/.test(s)) return { kind: "cancel" };

  if (/\b(reset|clear|release|disable|deactivate|turn off|switch off|unlock|override|bypass)\b.*\b(kill|switch|emergency|halt|limit|safety)\b|\bresume trading\b|\bunhalt\b/.test(s))
    return { kind: "refuse_reset" };
  if (!QUESTION.test(s) && /\b(buy|sell|short|go long|go short|place (an? )?order|square off|exit (my|the|all) positions?)\b/.test(s))
    return { kind: "refuse_order" };

  const op = KILL.some((r) => r.test(s)) ? "kill" : REPLAY.some((r) => r.test(s)) ? "replay" : RESEARCH.some((r) => r.test(s)) ? "research" : null;
  if (op) {
    if (confidence !== undefined && confidence < MIN_CONFIDENCE)
      return { kind: "clarify", reason: `I'm not sure I heard "${raw.trim()}" correctly, so I did nothing. Please say it again.` };
    return { kind: op } as Intent;
  }
  if (OPERATIONAL_WORDS.test(s) && !QUESTION.test(s)) {
    if (NEGATION.test(s)) return { kind: "clarify", reason: "I heard a negation, so I did nothing." };
    if (/\b(kill|halt|emergency|stop)\b/.test(s)) return { kind: "clarify", reason: 'To halt everything, say exactly "engage kill switch", or use the Emergency stop button.' };
    if (/\breplay\b/.test(s)) return { kind: "clarify", reason: 'To start a replay, say "run replay".' };
    if (/\bresearch\b/.test(s)) return { kind: "clarify", reason: 'To start research, say "run research".' };
  }

  if (/\b(status|report|how are we|how is everything|system check)\b/.test(s) && !NEGATION.test(s)) return { kind: "status" };
  if (/\b(news|headlines?)\b/.test(s)) return { kind: "news" };
  if (/\b(portfolio|equity|positions?|cash|p and l|pnl|profit)\b/.test(s) && !QUESTION.test(s.replace(/^how is /, "x "))) return { kind: "portfolio" };
  const open = s.match(/^(open|show( me)?|go to) (.+)$/);
  if (open) {
    const hit = PAGES.find(([re]) => re.test(open[3]));
    return hit ? { kind: "open", path: hit[1], name: hit[2] } : { kind: "clarify", reason: `I don't know a page called "${open[3]}".` };
  }
  return { kind: "ask", text: raw.trim() };
}

export const describeOperational = (k: string) =>
  k === "kill" ? "engage the kill switch and halt all paper trading" : k === "replay" ? "start a five-session screening replay" : "start a research run";
