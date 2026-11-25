/* One list for the sidebar AND the page headings: the same name in both places (e2e-tested).
   URLs are unchanged (bookmarks keep working); only names and grouping follow the operator's tasks. */
export const NAV: [string, [string, string][]][] = [
  ["Monitor", [["/hud", "Mission Control"], ["/today", "My Trading"], ["/review", "Daily Review"], ["/", "Command Center"], ["/market", "Market Intelligence"], ["/deck", "Vega Deck"]]],
  ["Strategies", [["/universe", "Strategy Universe"], ["/screening", "Strategy Monitor"], ["/contest", "Incumbent vs Challenger"],
    ["/factory", "Strategy Factory"], ["/code", "Strategy Code"]]],
  ["Research", [["/charts", "Chart Analyst"], ["/fundamental", "Fundamental Agent"], ["/backtest", "Backtesting Studio"], ["/walkforward", "Walk-Forward Testing"], ["/compare", "Strategy Comparison"], ["/replay", "Trade Replay"],
    ["/memory", "Experiment Memory"], ["/learning", "AI Research Brain"], ["/agents", "AI Research Agents"], ["/chat", "Analyst Chat"]]],
  ["Portfolio & Risk", [["/portfolio", "Portfolio Simulation"], ["/paper", "Paper Trading"], ["/risk", "Risk Control"], ["/brokers", "Broker Connections"]]],
  ["Investing", [["/funds", "Mutual Funds"], ["/planner", "SIP & Goal Planner"]]],
  ["Operations", [["/data", "Data Quality"], ["/system", "Operations"], ["/settings", "Configuration & Audit"]]],
];

export const TITLE: Record<string, string> = Object.fromEntries(NAV.flatMap(([, items]) => items));
