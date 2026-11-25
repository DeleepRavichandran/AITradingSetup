/* One list for the rail, the area tabs AND the page headings: the same name in every place (e2e-tested).
   Six areas (VEGA): Overview, Radar, Portfolio, Research, Risk, System. Every existing page stays reachable; URLs of
   existing pages are unchanged except the old Command Center, which moved to /command when "/" became the overview. */
export type Area = { key: string; label: string; pages: [string, string][] };

export const AREAS: Area[] = [
  { key: "overview", label: "Overview", pages: [["/", "Mission Control"], ["/hud", "Account Core"], ["/today", "My Trading"], ["/review", "Daily Review"],
    ["/command", "Command Center"], ["/deck", "Vega Deck"]] },
  { key: "radar", label: "Radar", pages: [["/radar", "Opportunity Radar"], ["/heatmaps", "Heatmap Intelligence"], ["/charts", "Chart Analyst"], ["/fundamental", "Fundamental Agent"],
    ["/market", "Market Intelligence"], ["/screening", "Strategy Monitor"], ["/universe", "Strategy Universe"]] },
  { key: "portfolio", label: "Portfolio", pages: [["/book", "Paper Portfolio"], ["/paper", "Paper Trading"], ["/portfolio", "Portfolio Simulation"],
    ["/funds", "Mutual Funds"], ["/planner", "SIP & Goal Planner"]] },
  { key: "research", label: "Research", pages: [["/integrity", "Evidence Integrity"], ["/brain", "Brain Intelligence"], ["/contest", "Incumbent vs Challenger"], ["/learning", "AI Research Brain"],
    ["/backtest", "Backtesting Studio"], ["/walkforward", "Walk-Forward Testing"], ["/compare", "Strategy Comparison"], ["/replay", "Trade Replay"],
    ["/memory", "Experiment Memory"], ["/factory", "Strategy Factory"], ["/code", "Strategy Code"], ["/agents", "AI Research Agents"], ["/chat", "Analyst Chat"]] },
  { key: "risk", label: "Risk", pages: [["/risk", "Risk Control"], ["/brokers", "Broker Connections"]] },
  { key: "system", label: "System", pages: [["/system", "Operations"], ["/data", "Data Quality"], ["/settings", "Configuration & Audit"]] },
];

export const NAV: [string, [string, string][]][] = AREAS.map((a) => [a.label, a.pages]);
export const TITLE: Record<string, string> = Object.fromEntries(NAV.flatMap(([, items]) => items));
export const areaOf = (path: string): Area => AREAS.find((a) => a.pages.some(([h]) => h === path)) || AREAS[0];
