import { useEffect, useState, useCallback, useRef } from "react";
import { fetchJSON, newsFeed, watchlist, tickerStrip, events } from "../api.js";
import { loadSessions, deleteSession } from "../agentHistory.js";
import { useApp } from "../App.jsx";
import { verdictBadge, verdictClass, SectionHeader, StatusIndicator } from "./ui.jsx";
import SecurityLink from "./SecurityLink.jsx";
import AgentPanel from "./AgentPanel.jsx";
import AgentChat from "./AgentChat.jsx";

function num(v, d = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
}

function pct(v) {
  const n = num(v);
  return `${n > 0 ? "+" : ""}${(n * 100).toFixed(2)}%`;
}

// Regime label from the already-loaded index snapshots (same thresholds as the
// backend's market-regime signal: S&P change vs ±0.5%, VIX 25 = elevated vol).
function regimeOf(indexes) {
  const arr = indexes || [];
  const spx = arr.find((i) => i.symbol === "^GSPC") || arr.find((i) => i.symbol === "SPY");
  const vix = arr.find((i) => i.symbol === "^VIX");
  const chg = spx ? num(spx.change_pct) : null;
  const vixVal = vix ? num(vix.close) : null;
  if (chg == null) return null;
  let label = chg > 0.5 ? "RISK-ON" : chg < -0.5 ? "RISK-OFF" : "CHOPPY";
  if (vixVal && vixVal > 25) label += " · HIGH VOL";
  return { label, chg, vix: vixVal };
}

// Decision-oriented event types for the alerts rail (deterministic backend keys).
const ALERT_TYPES = new Set([
  "committee_change",
  "committee_reversal",
  "signal_change",
  "significant_move",
  "volume_spike",
  "important_news",
  "significant_trade",
  "position_reversed",
]);

// Clever, rotating greeting lines (grouped with a matching emoji). One is picked
// every few minutes so it feels alive instead of a static time-of-day phrase.
const GREETINGS = [
  { icon: "🧗", opener: "Rise and grind", question: "What's the play?" },
  { icon: "🕵️", opener: "Still watching", question: "Where's the edge?" },
  { icon: "⚡", opener: "Market's restless", question: "What's the move?" },
  { icon: "☕", opener: "Coffee's cold", question: "But the tape never sleeps." },
  { icon: "📈", opener: "Overnight printed", question: "Let's read the tape." },
  { icon: "🔍", opener: "Scanning the board", question: "What lights up?" },
  { icon: "🌅", opener: "Alright", question: "Let's see what the market cooked up." },
  { icon: "🎯", opener: "The gods of alpha smile on you", question: "What's next?" },
];

function greeting() {
  const idx = Math.floor(Date.now() / 60000 / 5) % GREETINGS.length;
  return GREETINGS[idx];
}

const SHORTCUTS = [
  { key: "stock", label: "stocks" },
  { key: "crypto", label: "crypto" },
  { key: "forex", label: "Forex pairs" },
  { key: "etf", label: "ETFs & funds" },
  { key: "index", label: "indices" },
  { key: "futures", label: "futures" },
  { key: "bond", label: "bonds" },
  { key: "portfolio", label: "portfolio" },
  { key: "watchlist", label: "watchlist" },
];

export default function OverviewTab() {
  const { indexes, refreshToken, openDrawer, userEmail, username, addToPortfolio, removeFromPortfolio, markets } = useApp();
  const [verdicts, setVerdicts] = useState([]);
  const [news, setNews] = useState([]);
  const [watch, setWatch] = useState([]);
  const [active, setActive] = useState([]);
  const [alerts, setAlerts] = useState([]);
  const [error, setError] = useState("");
  const [chatOpen, setChatOpen] = useState(false);
  const [chatMode, setChatMode] = useState("AUTO");
  const [chatProvider, setChatProvider] = useState("auto");
  const [chatModel, setChatModel] = useState("");
  const [seed, setSeed] = useState("");
  const [seedId, setSeedId] = useState(0);
  const [searchMode, setSearchMode] = useState("");
  const [sessions, setSessions] = useState(() => loadSessions());
  const [restore, setRestore] = useState(null);
  const restoreNonce = useRef(0);
  const [wlTicker, setWlTicker] = useState("");
  const [wlMarket, setWlMarket] = useState("US");
  const [wlBusy, setWlBusy] = useState(false);

  useEffect(() => {
    if (markets?.length && !markets.some((m) => m.code === wlMarket)) {
      setWlMarket(markets[0].code);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [markets]);

  const openChat = (mode, provider, model, search) => {
    setChatMode(mode || "AUTO");
    setChatProvider(provider || "auto");
    setChatModel(model || "");
    setSearchMode(search || "");
    setChatOpen(true);
  };
  const askChat = (prompt, mode, provider, model, search) => {
    setChatMode(mode || "AUTO");
    setChatProvider(provider || "auto");
    setChatModel(model || "");
    setSearchMode(search || "");
    setSeed(prompt);
    setSeedId((n) => n + 1);
    setChatOpen(true);
  };

  const closeChat = useCallback(() => {
    setChatOpen(false);
    setSeed("");
    setSessions(loadSessions());
  }, []);

  const openSession = (s) => {
    setChatMode(s.mode || "AUTO");
    setChatProvider(s.provider || "auto");
    setSeed("");
    restoreNonce.current += 1;
    setRestore({ session: s, nonce: restoreNonce.current });
    setChatOpen(true);
  };

  const removeSession = (e, id) => {
    e.stopPropagation();
    deleteSession(id);
    setSessions(loadSessions());
  };

  const loadVerdicts = useCallback(() => {
    setError("");
    fetchJSON("/api/verdicts")
      .then((d) => setVerdicts(Object.values(d)))
      .catch((e) => setError(e.message));
  }, []);

  const loadWatch = useCallback(() => {
    watchlist().then(setWatch).catch(() => {});
  }, []);

  const loadRails = useCallback(() => {
    newsFeed(30).then(setNews).catch(() => {});
    loadWatch();
    tickerStrip()
      .then((rows) =>
        setActive(
          [...(rows || [])]
            .filter((r) => r.change_pct != null)
            .sort((a, b) => (b.change_pct || 0) - (a.change_pct || 0))
            .slice(0, 6)
        )
      )
      .catch(() => {});
  }, [loadWatch]);

  // Command-center alerts: ranked events, filtered to decision-oriented types.
  const loadAlerts = useCallback(() => {
    events(40)
      .then((all) =>
        setAlerts(
          (all || []).filter(
            (e) =>
              ALERT_TYPES.has(e.type) ||
              (e.type === "news" && (e.importance === "HIGH" || e.importance === "IMPORTANT"))
          ).slice(0, 6)
        )
      )
      .catch(() => {});
  }, []);

  const addWatch = () => {
    const tk = wlTicker.trim().toUpperCase();
    if (!tk || wlBusy) return;
    setWlBusy(true);
    addToPortfolio(wlMarket, tk);
    setWlTicker("");
    // Give the backend a beat to register + analyse, then refresh the list.
    setTimeout(() => {
      watchlist().then(setWatch).catch(() => {});
      setWlBusy(false);
    }, 1200);
  };

  const removeWatch = (e, w) => {
    e.stopPropagation();
    removeFromPortfolio(w.market, w.ticker);
    setWatch((list) => list.filter((x) => !(x.market === w.market && x.ticker === w.ticker)));
  };

  useEffect(() => {
    loadVerdicts();
    loadRails();
    loadAlerts();
    const t = setInterval(() => { loadVerdicts(); loadRails(); loadAlerts(); }, 15000);
    return () => clearInterval(t);
  }, [loadVerdicts, loadRails, loadAlerts]);

  useEffect(() => {
    if (refreshToken) {
      loadVerdicts();
      loadRails();
      loadAlerts();
    }
  }, [refreshToken, loadVerdicts, loadRails, loadAlerts]);

  const topVerdicts = [...verdicts]
    .sort((a, b) => (b.confidence || 0) - (a.confidence || 0))
    .slice(0, 6);

  const strip = [...(indexes || [])].slice(0, 14);
  const greet = greeting();
  const regime = regimeOf(indexes);
  const openMarkets = (markets || []).filter((m) => m.status?.status === "open");
  const closedMarkets = (markets || []).filter((m) => m.status?.status !== "open");

  const openDossier = (v) =>
    openDrawer({
      type: "stock",
      v: { market: v.market, ticker: v.ticker, company: v.company || "", reason: ["OVERVIEW"] },
    });

  return (
    <div className="overview">
      <div className="ov-frame">
        {/* Top index strip */}
        <div className="ov-strip">
          {strip.map((s) => {
            const up = (s.change_pct || 0) >= 0;
            return (
              <span key={`${s.market}:${s.symbol}`} className="ov-strip-item" onClick={() => openDrawer({ type: "stock", v: { market: s.market, ticker: s.symbol, company: s.name || "", reason: ["OVERVIEW"] } })}>
                <span className="ov-strip-sym">{s.symbol}</span>
                <span className="ov-strip-px">{num(s.close).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
                <span className={`ov-strip-chg ${up ? "up" : "down"}`}>{pct(s.change_pct)}</span>
              </span>
            );
          })}
        </div>

        {/* Command center strip: market status + regime (existing data only) */}
        <div className="ov-strip" style={{ opacity: 0.95 }}>
          {(openMarkets.length ? openMarkets : closedMarkets).slice(0, 6).map((m) => {
            const open = m.status?.status === "open";
            return (
              <span key={m.code} className="ov-strip-item" title={`${m.name} · ${m.status?.local_time || ""} local`}>
                <span className={`dot ${open ? "up" : "dim"}`} style={{ marginRight: 4 }}>●</span>
                <span className="ov-strip-sym">{m.code}</span>
                <span className={`dim ${open ? "up" : ""}`}>{open ? "OPEN" : "CLOSED"}</span>
              </span>
            );
          })}
          {regime && (
            <span className="ov-strip-item" title={`S&P ${pct(regime.chg)} · VIX ${regime.vix ?? "—"}`}>
              <span className="ov-strip-sym">REGIME</span>
              <span className={regime.label.startsWith("RISK-ON") ? "up" : regime.label.startsWith("RISK-OFF") ? "down" : "dim"}>
                {regime.label} {regime.chg != null ? `(${pct(regime.chg)})` : ""}
              </span>
            </span>
          )}
        </div>

        <div className="ov-body">
          {/* LEFT MAIN */}
          <main className="ov-main">
            {/* Shortcuts */}
            <div className="ov-shortcuts">
              <div className="ov-panel-label ov-label-chev">
                SHORTCUTS <span className="ov-chev">❯</span>
              </div>
              <div className="ov-shortcut-grid">
                {SHORTCUTS.map((s) => (
                  <span key={s.key} className="ov-shortcut">
                    <span className="ov-shortcut-token">/{s.key}</span>
                    <span className="ov-shortcut-label">{s.label}</span>
                  </span>
                ))}
              </div>
            </div>

            {/* Greeting */}
            <header className="ov-greet">
              <span className="ov-greet-text">
                <strong>Market workspace</strong>
                <span className="dim"> / {username || userEmail?.split("@")[0] || "Overview"}</span>
              </span>
              <button className="ov-setup">⛶ Set Up Profile</button>
            </header>

            {/* Agent */}
            <AgentPanel onOpen={openChat} onAsk={askChat} />

            {/* Committee views */}
            <div className="ov-card">
              <SectionHeader title="TOP COMMITTEE VIEWS" />
              {topVerdicts.length ? (
                <div className="ov-verdict-grid">
                  {topVerdicts.map((v) => (
                    <div key={`${v.market}:${v.ticker}`} className={`ov-panel ${verdictClass(v.verdict)}`} onClick={() => openDossier(v)}>
                      <div className="ov-panel-head">
                        <div>
                          <SecurityLink market={v.market} ticker={v.ticker} className="symbol" style={{ fontSize: 13 }}>{v.ticker}</SecurityLink>
                          <div className="name">{v.market}</div>
                        </div>
                        {verdictBadge(v)}
                      </div>
                      <div className="conf-bar">
                        <span style={{ width: (v.confidence * 100).toFixed(0) + "%", background: v.verdict === "BULL" ? "var(--bull)" : v.verdict === "BEAR" ? "var(--bear)" : "var(--neutral)" }} />
                      </div>
                      <div className="row"><span className="label">CONFIDENCE</span><span className="value">{(v.confidence * 100).toFixed(0)}%</span></div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="empty">NO VERDICTS YET — RUN A REFRESH.</div>
              )}
            </div>
          </main>

          {/* RIGHT RAIL */}
          <aside className="ov-rail">
            <div className="ov-rail-box">
              <div className="ov-panel-label">ALERTS &amp; SIGNALS <span className="dim">· LIVE</span></div>
              {alerts.length ? (
                <div className="ov-news-list">
                  {alerts.map((a) => (
                    <div key={a.id} className="ov-news-item">
                      <div className="ov-news-meta dim" style={{ marginBottom: 2 }}>
                        <span>{String(a.type || "").replace(/_/g, " ").toUpperCase()}</span>
                        {a.security_id && (
                          <SecurityLink securityId={a.security_id} className="ov-news-tk">
                            {a.security_id}
                          </SecurityLink>
                        )}
                        <span>{String(a.timestamp || "").slice(0, 16).replace("T", " ")}</span>
                      </div>
                      <span className="ov-news-title" style={{ cursor: a.security_id ? "pointer" : "default" }}
                        onClick={() => {
                          if (!a.security_id) return;
                          const [mkt, ...rest] = String(a.security_id).split(":");
                          if (mkt && rest.length) openDrawer({ type: "stock", v: { market: mkt, ticker: rest.join(":"), company: "", reason: ["ALERT"] } });
                        }}
                        title={a.security_id ? `Open Dossier ${a.security_id}` : a.headline}
                      >
                        {a.headline}
                      </span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="empty">NO ALERTS — COMMITTEE CHANGES, SIGNAL FLIPS, MOVES AND IMPORTANT NEWS APPEAR HERE.</div>
              )}
            </div>

            <div className="ov-rail-box ov-news-box">
              <div className="ov-panel-label">LIVE NEWS <span className="dim">· SCROLL</span></div>
              {news.length ? (
                <div className="ov-news-list">
                  {news.map((n, i) => {
                    const isGlobal = n.market === "GLOBAL" || n.ticker === "NEWS";
                    return (
                      <div key={`${n.url}-${i}`} className="ov-news-item">
                        <a
                          className="ov-news-title"
                          href={n.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          title={n.title}
                        >
                          {n.title}
                        </a>
                        {n.summary && String(n.summary).toLowerCase() !== String(n.title || "").toLowerCase() ? (
                          <div className="ov-news-summary">{n.summary}</div>
                        ) : (
                          <div className="ov-news-summary dim">No summary — open for the full report.</div>
                        )}
                        <div className="ov-news-meta dim">
                          <span>{n.source}</span>
                          {!isGlobal && (n.ticker || n.security_id) && (
                            <span className="ov-news-tk">{n.ticker || n.security_id}</span>
                          )}
                          <span>{String(n.published_at || n.fetched_at || "").slice(0, 10)}</span>
                          <a
                            className="ov-news-open"
                            href={n.url}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            READ ⟶
                          </a>
                        </div>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="empty">LOADING NEWS…</div>
              )}
            </div>

            <div className="ov-rail-box">
              <div className="ov-panel-label">WATCHLIST</div>
              <div className="ov-watch-add">
                <select
                  className="ov-watch-market"
                  value={wlMarket}
                  onChange={(e) => setWlMarket(e.target.value)}
                  title="Market"
                >
                  {(markets?.length ? markets : [{ code: "US", name: "United States" }]).map((m) => (
                    <option key={m.code} value={m.code}>{m.code}</option>
                  ))}
                </select>
                <input
                  className="ov-watch-input"
                  placeholder="TICKER (e.g. AAPL)"
                  value={wlTicker}
                  onChange={(e) => setWlTicker(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") addWatch();
                  }}
                />
                <button className="ov-watch-addbtn" onClick={addWatch} disabled={wlBusy}>
                  {wlBusy ? "…" : "+ ADD"}
                </button>
              </div>
              {watch.length ? (
                <div className="ov-watch-list">
                  {watch.slice(0, 6).map((w) => (
                    <div key={`${w.market}:${w.ticker}`} className="ov-watch-item" onClick={() => openDossier(w)}>
                      <span className="ov-watch-tk">{w.ticker}</span>
                      <span className="ov-watch-name dim">{w.market}</span>
                      {w.verdict && (
                        <span className={`ov-watch-verdict ${verdictClass(w.verdict)}`}>{w.verdict}</span>
                      )}
                      <button className="ov-watch-x" title={`Remove ${w.ticker} from watchlist`} onClick={(e) => removeWatch(e, w)}>✕</button>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="empty">NO WATCHLIST ITEMS.</div>
              )}
            </div>

            <div className="ov-rail-box">
              <div className="ov-panel-label">AGENT HISTORY</div>
              {sessions.length ? (
                <div className="ov-hist-list">
                  {sessions.map((s) => (
                    <div key={s.id} className="ov-hist-item" onClick={() => openSession(s)} title="Reopen this agent session">
                      <span className="ov-hist-title">{s.title || "Untitled session"}</span>
                      <span className="ov-hist-meta dim">
                        {String(s.updated_at || "").slice(0, 10)}
                        <button className="ov-watch-x" title="Delete session" onClick={(e) => removeSession(e, s.id)}>✕</button>
                      </span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="empty">NO SAVED SESSIONS YET.</div>
              )}
            </div>

            <div className="ov-rail-box">
              <div className="ov-panel-label">MOST ACTIVE</div>
              {active.length ? (
                <div className="ov-active-list">
                  {active.map((a, i) => {
                    const up = (a.change_pct || 0) >= 0;
                    return (
                      <div key={a.security_id || i} className="ov-active-item" onClick={() => openDossier(a)}>
                        <span className="ov-active-tk">{a.ticker}</span>
                        <span className="ov-active-px">{num(a.close).toLocaleString()}</span>
                        <span className={`ov-active-chg ${up ? "up" : "down"}`}>{pct(a.change_pct)}</span>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="empty">LOADING…</div>
              )}
            </div>
          </aside>
        </div>
      </div>

      <AgentChat
        open={chatOpen}
        onClose={closeChat}
        seed={seed}
        seedId={seedId}
        initialMode={chatMode}
        initialProvider={chatProvider}
        initialModel={chatModel}
        initialSearch={searchMode}
        onProviderChange={setChatProvider}
        restoreSession={restore?.session || null}
        restoreNonce={restore?.nonce || 0}
      />
    </div>
  );
}
