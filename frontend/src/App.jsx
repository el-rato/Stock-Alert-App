import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { fetchJSON, apiUrl, authMe, authLogout } from "./api.js";
import { getDossierPath, parseDossierHash, securityIdOf, splitSecurityId } from "./nav.js";
import Landing from "./components/Landing.jsx";
import AuthScreen from "./components/AuthScreen.jsx";
import OverviewTab from "./components/OverviewTab.jsx";
import PortfolioTab from "./components/PortfolioTab.jsx";
import ScannerTab from "./components/ScannerTab.jsx";
import ScreenerTab from "./components/ScreenerTab.jsx";
import FundsTab from "./components/FundsTab.jsx";
import SimulationTab from "./components/SimulationTab.jsx";
import PaperTab from "./components/PaperTab.jsx";
import PaperOrderPanel from "./components/PaperOrderPanel.jsx";
import NewsTab from "./components/NewsTab.jsx";
import WorkflowTab from "./components/WorkflowTab.jsx";
import PortfolioGroups from "./components/PortfolioGroups.jsx";
import NotificationsBell from "./components/NotificationsBell.jsx";
import AlertsTab from "./components/AlertsTab.jsx";
import Drawer from "./components/Drawer.jsx";
import ErrorBoundary from "./components/ErrorBoundary.jsx";
import SearchBox from "./components/SearchBox.jsx";
import SecurityLink from "./components/SecurityLink.jsx";

// Data-health chip state (kept out of context: only the footer reads it).
function useDataHealth(enabled) {
  const [health, setHealth] = useState(null);
  useEffect(() => {
    if (!enabled) return undefined;
    let alive = true;
    const load = () =>
      fetchJSON("/api/health/data")
        .then((d) => { if (alive) setHealth(d); })
        .catch(() => {});
    load();
    const t = setInterval(load, 60000);
    return () => { alive = false; clearInterval(t); };
  }, [enabled]);
  return health;
}

export const AppContext = createContext(null);
export const useApp = () => useContext(AppContext);

const PRIMARY_TABS = [
  { key: "overview", fn: "F1", label: "OVERVIEW" },
  { key: "scanner", fn: "F2", label: "SCANNER" },
  { key: "portfolio", fn: "F3", label: "PORTFOLIO" },
  { key: "alerts", fn: "F4", label: "ALERTS" },
  { key: "paper", fn: "F5", label: "PAPER" },
];

const SECONDARY_TABS = [
  { key: "news", fn: "F6", label: "NEWS" },
  { key: "screener", fn: "F7", label: "SCREENER" },
  { key: "sim", fn: "F8", label: "SIM / BACKTEST" },
  { key: "funds", fn: "F9", label: "HEDGE FUNDS" },
  { key: "workflows", fn: "F10", label: "WORKFLOWS" },
];

const TAB_COMPONENTS = {
  overview: OverviewTab,
  alerts: AlertsTab,
  portfolio: PortfolioTab,
  scanner: ScannerTab,
  screener: ScreenerTab,
  paper: PaperTab,
  news: NewsTab,
  sim: SimulationTab,
  funds: FundsTab,
  workflows: WorkflowTab,
};

const CURRENCY_SYMBOLS = {
  GBP: "£",
  USD: "$",
  EUR: "€",
  JPY: "¥",
  KRW: "₩",
  INR: "₹",
  HKD: "HK$",
  SGD: "S$",
  AUD: "A$",
  CAD: "C$",
  CHF: "CHF ",
  SEK: "kr ",
};

function useClock() {
  const [now, setNow] = useState(new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  return now;
}

function TickerTape({ tickers }) {
  const items = useMemo(() => {
    const arr = [...(tickers || [])];
    for (let i = arr.length - 1; i > 0; i -= 1) {
      const j = Math.floor(Math.random() * (i + 1));
      [arr[i], arr[j]] = [arr[j], arr[i]];
    }
    return arr.slice(0, 60);
  }, [tickers]);
  if (!items.length) return <div className="ticker-tape" />;
  const duration = Math.max(60, items.length * 1.5);
  const render = (s, i) => {
    const up = (s.change_pct ?? 0) >= 0;
    const sym = CURRENCY_SYMBOLS[s.currency] || "";
    const price = Number(s.close || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
    return (
      <SecurityLink key={i} market={s.market} ticker={s.ticker} className="tape-item" title={`Open Dossier ${s.security_id}`}>
        <span className="t">{s.security_id}</span>{" "}
        {sym}{price}{" "}
        {s.change_pct == null ? (
          <span className="dim">NO_DATA</span>
        ) : (
          <span className={up ? "up" : "down"}>
            {up ? "+" : ""}
            {(s.change_pct * 100).toFixed(2)}%
          </span>
        )}
      </SecurityLink>
    );
  };
  return (
    <div className="ticker-tape">
      <div className="tape-inner" style={{ animationDuration: `${duration}s` }}>
        {[...items, ...items].map(render)}
      </div>
    </div>
  );
}

export default function App() {
  const [auth, setAuth] = useState({ status: "checking", user: null });
  const [authMode, setAuthMode] = useState(null);
  const [market, setMarket] = useState("");
  const [markets, setMarkets] = useState([]);
  const [indexes, setIndexes] = useState([]);
  const [tickers, setTickers] = useState([]);
  const [tab, setTab] = useState("overview");
  const [drawer, setDrawer] = useState(null);
  const [paperTicket, setPaperTicket] = useState(null);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [refreshToken, setRefreshToken] = useState(0);
  const [refreshStatus, setRefreshStatus] = useState({
    running: false,
    last_fast_at: null,
    last_slow_at: null,
    next_fast_in: 0,
    next_slow_in: 0,
    error: "",
  });
  const [theme, setTheme] = useState(() => localStorage.getItem("sv-theme") || "system");
  const [resolvedTheme, setResolvedTheme] = useState("dark");
  const [portfolioIds, setPortfolioIds] = useState(new Set());
  const [moreOpen, setMoreOpen] = useState(false);
  const [screenerPrefill, setScreenerPrefill] = useState(null);
  const moreRef = useRef(null);
  const now = useClock();
  const refreshInFlight = useRef(false);
  const health = useDataHealth(auth.status === "authed");
  // Tracks a stock-dossier open initiated by openDrawer so the hashchange
  // handler does not clobber the rich drawer payload with a minimal one.
  const lastOpenRef = useRef(null);
  // Previous hash: lets onHash distinguish an intentional leave (dossier ->
  // something else, e.g. browser back) from an unrelated hash write that must
  // NOT close the open Dossier (News/Committee/Researcher/Signals stay open).
  const prevHashRef = useRef(window.location.hash);

  // Canonical Dossier route: #/dossier/{security_id}. The hash is the single
  // source of truth for stock dossiers: SecurityLink anchors, openDrawer calls
  // and browser back/forward all converge here, then open the same Drawer.
  const openDrawer = useCallback((d) => {
    if (d && d.type === "stock" && d.v) {
      const id = securityIdOf(d.v.market, d.v.ticker);
      if (id) {
        setDrawer(d);
        const path = getDossierPath(id);
        if (window.location.hash !== path) {
          lastOpenRef.current = { id };
          window.location.hash = path;
        }
        prevHashRef.current = window.location.hash;
        return;
      }
    }
    lastOpenRef.current = null;
    setDrawer(d);
    // A non-stock panel (e.g. a fund) must not leave a stale dossier hash
    // behind: a later hashchange would otherwise resurrect the stock Dossier
    // and clobber this panel's state.
    if (parseDossierHash(window.location.hash)) {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    prevHashRef.current = window.location.hash;
  }, []);

  const closeDrawer = useCallback(() => {
    setDrawer(null);
    if (parseDossierHash(window.location.hash)) {
      // Collapse the dossier URL on close without adding a history entry.
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    prevHashRef.current = window.location.hash;
  }, []);

  useEffect(() => {
    const onHash = () => {
      const prevWasDossier = !!parseDossierHash(prevHashRef.current);
      prevHashRef.current = window.location.hash;
      const id = parseDossierHash(window.location.hash);
      if (!id) {
        // Only an intentional leave (a previous dossier hash navigating away,
        // e.g. browser back) closes the panel. Unrelated hash writes never do.
        if (prevWasDossier) {
          setDrawer((cur) => (cur && cur.type === "stock" ? null : cur));
        }
        return;
      }
      const last = lastOpenRef.current;
      if (last && last.id === id) {
        lastOpenRef.current = null; // already opened by openDrawer with rich data
        return;
      }
      const known = (markets || []).map((m) => m.code);
      const { market, ticker } = splitSecurityId(id, known);
      if (!market || !ticker) return; // never open a broken dossier route
      setDrawer({ type: "stock", v: { market, ticker, company: "", reason: ["DOSSIER LINK"] } });
    };
    window.addEventListener("hashchange", onHash);
    if (parseDossierHash(window.location.hash)) onHash(); // deep link on load
    return () => window.removeEventListener("hashchange", onHash);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [markets]);

  useEffect(() => {
    const onDoc = (e) => {
      if (moreRef.current && !moreRef.current.contains(e.target)) setMoreOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  // Resolve dark/light/system and apply to <html data-theme>.
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: light)");
    const apply = () => {
      const resolved = theme === "system" ? (media.matches ? "light" : "dark") : theme;
      document.documentElement.setAttribute("data-theme", resolved);
      setResolvedTheme(resolved);
    };
    apply();
    localStorage.setItem("sv-theme", theme);
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [theme]);

  const loadPortfolio = useCallback(() => {
    fetchJSON("/api/watchlist")
      .then((list) => setPortfolioIds(new Set((list || []).map((w) => `${w.market}:${w.ticker}`))))
      .catch(() => {});
  }, []);

  useEffect(() => {
    loadPortfolio();
  }, [loadPortfolio]);

  useEffect(() => {
    if (refreshToken) loadPortfolio();
  }, [refreshToken, loadPortfolio]);

  const addToPortfolio = useCallback(
    (market, ticker, company = "") => {
      const id = `${market}:${ticker}`;
      setPortfolioIds((s) => new Set(s).add(id));
      fetchJSON("/api/watchlist?analyze=0", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ market, ticker, company }),
      })
        .catch(() => setPortfolioIds((s) => { const n = new Set(s); n.delete(id); return n; }));
    },
    []
  );

  const removeFromPortfolio = useCallback((market, ticker) => {
    const id = `${market}:${ticker}`;
    setPortfolioIds((s) => { const n = new Set(s); n.delete(id); return n; });
    fetchJSON(`/api/watchlist?market=${encodeURIComponent(market)}&ticker=${encodeURIComponent(ticker)}`, { method: "DELETE" }).catch(() => {});
  }, []);

  const inPortfolio = useCallback((market, ticker) => portfolioIds.has(`${market}:${ticker}`), [portfolioIds]);

  useEffect(() => {
    fetchJSON("/api/markets")
      .then(setMarkets)
      .catch(() => setMarkets([]));
  }, []);

  // Initial session check: decides landing vs. terminal without a visible
  // landing->redirect flicker.
  useEffect(() => {
    authMe()
      .then((d) => setAuth({ status: "authed", user: d.user || null }))
      .catch(() => setAuth({ status: "anon", user: null }));
  }, []);

  const handleAuthSuccess = useCallback(async () => {
    try {
      const d = await authMe();
      setAuth({ status: "authed", user: d.user || null });
      setAuthMode(null);
      setTab("overview");
    } catch {
      setAuth({ status: "anon", user: null });
    }
  }, []);

  const handleLogout = useCallback(async () => {
    await authLogout().catch(() => {});
    setAuth({ status: "anon", user: null });
    setAuthMode(null);
  }, []);


  const loadIndexes = () => {
    fetchJSON("/api/indexes")
      .then(setIndexes)
      .catch(() => {});
  };

  const loadTickers = () => {
    fetchJSON("/api/ticker-strip")
      .then(setTickers)
      .catch(() => {});
  };

  useEffect(() => {
    loadIndexes();
    const t = setInterval(loadIndexes, 60000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    loadTickers();
    const t = setInterval(loadTickers, 60000);
    return () => clearInterval(t);
  }, []);

  const runBackgroundRefresh = useCallback(() => {
    if (refreshInFlight.current) return;
    refreshInFlight.current = true;
    setRefreshStatus((prev) => ({ ...prev, running: true, error: "" }));
    fetch(apiUrl("/api/refresh"), { method: "POST" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((payload) => {
        setRefreshStatus(payload);
        setRefreshToken((t) => t + 1);
        setLastUpdated(new Date());
      })
      .catch(() => {
        setRefreshStatus((prev) => ({ ...prev, running: false, error: "update failed" }));
      })
      .finally(() => {
        refreshInFlight.current = false;
      });
  }, []);

  useEffect(() => {
    if (auth.status !== "authed") return undefined;
    runBackgroundRefresh();
    const t = setInterval(runBackgroundRefresh, 15000);
    return () => clearInterval(t);
  }, [auth.status, runBackgroundRefresh]);

  // Shared SecurityContext: the canonical selected security (if any). Dossier,
  // News, Committee and Paper all read this single source of truth rather than
  // each deriving their own market/ticker, so panels never drift apart.
  const security = useMemo(() => {
    if (drawer && drawer.type === "stock" && drawer.v) {
      const id = securityIdOf(drawer.v.market, drawer.v.ticker);
      return {
        security_id: id || "",
        market: drawer.v.market || "",
        ticker: drawer.v.ticker || "",
        symbol: drawer.v.symbol || "",
        company: drawer.v.company || "",
      };
    }
    return null;
  }, [drawer]);

  const ctx = useMemo(
    () => ({
      market,
      markets,
      indexes,
      security,
      theme,
      setTheme,
      setMarket,
      setTab,
      userEmail: auth.user?.email || "",
      username: auth.user?.username || "",
      refreshAll: () => {
        setRefreshToken((t) => t + 1);
        setLastUpdated(new Date());
      },
      refreshToken,
      refreshStatus,
      openDrawer: (d) => openDrawer(d),
      openPaperTicket: (t) => setPaperTicket(t),
      portfolioIds,
      addToPortfolio,
      removeFromPortfolio,
      inPortfolio,
      screenerPrefill,
      setScreenerPrefill,
    }),
    [market, markets, indexes, security, refreshToken, refreshStatus, theme, portfolioIds, addToPortfolio, removeFromPortfolio, inPortfolio, screenerPrefill, openDrawer, auth]
  );

  const ActiveTab = TAB_COMPONENTS[tab];

  return (
    <AppContext.Provider value={ctx}>
      {auth.status === "checking" ? (
        <div className="boot"><span>SV · STOCK VERDICT</span></div>
      ) : auth.status === "anon" ? (
        authMode === "login" ? (
          <AuthScreen mode="login" onSwitch={setAuthMode} onSuccess={handleAuthSuccess} />
        ) : authMode === "register" ? (
          <AuthScreen mode="register" onSwitch={setAuthMode} onSuccess={handleAuthSuccess} />
        ) : (
          <Landing onLogin={() => setAuthMode("login")} onRegister={() => setAuthMode("register")} />
        )
      ) : (
        <div className="terminal">
          <header className="topbar">
            <button className="logo" onClick={() => setTab("overview")} aria-label="Open overview">
              <span className="brand-mark">M</span>
              <span className="brand-copy">MESH<small>MARKET INTELLIGENCE</small></span>
            </button>
            <TickerTape tickers={tickers} />
            <SearchBox />
            <NotificationsBell />
            <select className="theme-toggle" value={theme} onChange={(e) => setTheme(e.target.value)} title="Theme">
              <option value="dark">Dark</option>
              <option value="light">Light</option>
              <option value="system">System</option>
            </select>
            <div className="user-menu" title={auth.user?.email || ""}>
              <span className="user-email">{auth.user?.email || "ACCOUNT"}</span>
              <button className="ghost" onClick={handleLogout}>LOGOUT</button>
            </div>
            <span className="clock">{now.toLocaleTimeString()}</span>
          </header>

          <nav className="tabs">
            {PRIMARY_TABS.map((t) => (
              <button
                key={t.key}
                className={`fn-tab ${tab === t.key ? "active" : ""}`}
                onClick={() => setTab(t.key)}
              >
                <span className="fn">{t.fn}</span>
                {t.label}
              </button>
            ))}
            <div className="more-wrap" ref={moreRef}>
              <button
                className={`fn-tab more-btn ${SECONDARY_TABS.some((t) => t.key === tab) ? "active" : ""}`}
                onClick={() => setMoreOpen((v) => !v)}
              >
                MORE <span className="expand">{moreOpen ? "−" : "+"}</span>
              </button>
              {moreOpen && (
                <div className="more-menu">
                  {SECONDARY_TABS.map((t) => (
                    <button
                      key={t.key}
                      className={`more-item ${tab === t.key ? "active" : ""}`}
                      onClick={() => {
                        setTab(t.key);
                        setMoreOpen(false);
                      }}
                    >
                      <span className="fn">{t.fn}</span>
                      {t.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </nav>

          <div className="controls shell-controls">
            <div className="field">
              <label>Market</label>
              <select value={market} onChange={(e) => setMarket(e.target.value)}>
                <option value="">All markets</option>
                {markets.map((m) => (
                  <option key={m.code} value={m.code}>
                    {m.code} — {m.name}
                  </option>
                ))}
              </select>
            </div>
            <button className="primary" onClick={() => ctx.refreshAll()}>
              ⟳ Refresh data
            </button>
            <button className="ghost" onClick={() => setTab("overview")}>
              Market pulse
            </button>
          </div>
          <main className="content">
            <ErrorBoundary key={tab}>
              <ActiveTab />
            </ErrorBoundary>
          </main>

          <footer className="statusbar">
            <span>SV 0.1.0</span>
            <span className={now.getSeconds() % 2 ? "pulse" : ""}>● LIVE</span>
            {health && (
              <span
                className="dim"
                title={[
                  `providers: ${(health.providers || []).map((p) => `${p.name}${p.enabled ? (p.cooling_down ? " (cooling)" : "") : " (off)"}`).join(", ") || "n/a"}`,
                  `stale: ${health.counts?.stale ?? "—"} · NO_DATA: ${health.counts?.no_data ?? "—"} · ERROR: ${health.counts?.error ?? "—"}`,
                  `signal coverage: ${Object.entries(health.signal_coverage || {}).filter(([k]) => k !== "of_analyzed").map(([k, v]) => `${k} ${v}/${health.signal_coverage.of_analyzed ?? "—"}`).join(", ")}`,
                  `last snapshot: ${health.last_price_snapshot || "—"}`,
                  `warm queue: ${health.workers?.warm_queue_pending ?? "—"}`,
                ].join("\n")}
              >
                DATA {health.counts?.with_price_data ?? "—"}/{health.counts?.securities ?? "—"}
                {health.counts?.stale ? ` · STALE ${health.counts.stale}` : ""}
                {health.counts?.no_data ? ` · NO_DATA ${health.counts.no_data}` : ""}
                {health.counts?.error ? ` · ERR ${health.counts.error}` : ""}
              </span>
            )}
            <span>
              LAST UPDATED{" "}
              {lastUpdated ? lastUpdated.toLocaleTimeString() : "--:--:--"}
            </span>
            <span>MARKET: {market || "ALL"}</span>
            <span style={{ marginLeft: "auto" }}>
              {now.toLocaleDateString()} {now.toLocaleTimeString()}
            </span>
          </footer>
        </div>
      )}
      <ErrorBoundary key={drawer ? `${drawer.type}:${drawer.v?.ticker || drawer.s?.cik || "?"}` : "closed"}>
        <Drawer item={drawer} onClose={closeDrawer} />
      </ErrorBoundary>
      <ErrorBoundary key={paperTicket ? `${paperTicket.market}:${paperTicket.ticker}` : "closed"}>
        <PaperOrderPanel ticket={paperTicket} onClose={() => setPaperTicket(null)} />
      </ErrorBoundary>
    </AppContext.Provider>
  );
}
