import { useEffect, useState, useCallback, useRef } from "react";
import { fetchJSON, newsFeed, watchlist, tickerStrip, events } from "../api.js";
import { loadSessions, deleteSession } from "../agentHistory.js";
import { useApp } from "../App.jsx";
import { verdictBadge, verdictClass, SectionHeader, StatusIndicator } from "./ui.jsx";
import SecurityLink from "./SecurityLink.jsx";
import AgentPanel from "./AgentPanel.jsx";
import AgentChat from "./AgentChat.jsx";
import PriceChart from "./PriceChart.jsx";

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

export default function OverviewTab({ marketRows = [] }) {
  const { indexes, refreshToken, openDrawer, theme, portfolioIds, setTab, addToPortfolio, removeFromPortfolio, markets } = useApp();
  const [chartSelection, setChartSelection] = useState(null);
  const [chartRange, setChartRange] = useState("1mo");
  const chartSecurity = chartSelection || indexes?.[0];
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
    <div className="overview market-home">
      <header className="market-heading">
        <div><h1>Markets</h1><p>Your view of the trading day.</p></div>
        <details className="exchange-menu"><summary>Exchange status</summary>        {/* Command center strip: market status + regime (existing data only) */}
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

</details>
      </header>

      <div className="market-workspace">
        <section className="market-primary" aria-label="Market overview">
          <div className="market-index-selector" aria-label="Chart security">
            {strip.map((item) => (
              <button type="button" key={`${item.market}:${item.symbol}`}
                className={chartSecurity?.symbol === item.symbol && chartSecurity?.market === item.market ? "market-index selected" : "market-index"}
                onClick={() => setChartSelection(item)}
                aria-pressed={chartSecurity?.symbol === item.symbol && chartSecurity?.market === item.market}>
                <span>{item.name || item.symbol}</span>
                <strong>{num(item.close).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong>
                <small className={(item.change_pct || 0) >= 0 ? "up" : "down"}>{pct(item.change_pct)}</small>
              </button>
            ))}
          </div>
          <section className="market-chart-section" aria-label="Market movement">
            <div className="market-chart-heading">
              <div><h2>{chartSecurity?.name || chartSecurity?.symbol || "Market movement"}</h2><span className="dim">{chartSecurity?.market || "Select a market"} · Price movement</span></div>
              <div className="market-ranges" aria-label="Chart period">
                {[["1mo", "1M"], ["3mo", "3M"], ["6mo", "6M"], ["1y", "1Y"]].map(([value, label]) => (
                  <button key={value} type="button" onClick={() => setChartRange(value)} aria-pressed={chartRange === value}>{label}</button>
                ))}
              </div>
            </div>
            <div className="market-chart-canvas">
              {chartSecurity?.market && chartSecurity?.symbol ? (
                <PriceChart url={`/api/chart/${encodeURIComponent(chartSecurity.market)}/${encodeURIComponent(chartSecurity.symbol)}?range=${chartRange}`}
                  chartType="candlestick" showVolume refreshKey={refreshToken} theme={theme} />
              ) : <div className="workspace-empty"><strong>Market chart</strong><span>Available index data will appear here.</span></div>}
            </div>
          </section>
          <div className="market-lower">
            <section className="market-movers">            <div className="ov-rail-box">
              <div className="ov-panel-label">Market movers</div>
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
</section>
            <section className="market-news">            <div className="ov-rail-box ov-news-box">
              <div className="ov-panel-label">Market news</div>
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

</section>
          </div>
          <details className="secondary-signals"><summary>Committee views &amp; signals</summary>
                        {/* Committee views */}
            <div className="ov-card">
              <SectionHeader title="Committee views" />
              {topVerdicts.length ? (
                <div className="ov-verdict-table">
                  <table className="data-table">
                    <thead><tr><th>Security</th><th>Market</th><th>Verdict</th><th className="num">Confidence</th></tr></thead>
                    <tbody>
                  {topVerdicts.map((v) => (
                    <tr key={`${v.market}:${v.ticker}`} onClick={() => openDossier(v)}>
                      <td><SecurityLink market={v.market} ticker={v.ticker} className="symbol">{v.ticker}</SecurityLink></td>
                      <td className="dim">{v.market}</td>
                      <td>{verdictBadge(v)}</td>
                      <td className="num">{(v.confidence * 100).toFixed(0)}%</td>
                    </tr>
                  ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="empty">NO VERDICTS YET — RUN A REFRESH.</div>
              )}
            </div>
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


          </details>
        </section>

        <aside className="market-sidebar" aria-label="Your securities">
                      <div className="ov-rail-box">
              <div className="ov-panel-label">Watchlist</div>
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
                  placeholder="Add a ticker"
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
                <div className="watchlist-table-wrap">
                  <table className="watchlist-table">
                    <thead><tr><th>Ticker</th><th>Price</th><th>Change %</th><th><span className="sr-only">Actions</span></th></tr></thead>
                    <tbody>{watch.slice(0, 6).map((w) => {
                      const quote = marketRows.find((row) => row.market === w.market && row.ticker === w.ticker);
                      const price = quote?.close ?? w.close;
                      const change = quote?.change_pct ?? w.change_pct;
                      return (
                        <tr key={`${w.market}:${w.ticker}`} onClick={() => openDossier(w)}>
                          <td><SecurityLink market={w.market} ticker={w.ticker}>{w.ticker}</SecurityLink><small>{w.market}</small></td>
                          <td>{price == null ? "—" : Number(price).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                          <td className={change == null ? "dim" : change >= 0 ? "up" : "down"}>{change == null ? "—" : pct(change)}</td>
                          <td><button className="ov-watch-x" title={`Remove ${w.ticker} from watchlist`} onClick={(e) => removeWatch(e, w)}>×</button></td>
                        </tr>
                      );
                    })}</tbody>
                  </table>
                </div>
              ) : (
                <div className="empty">NO WATCHLIST ITEMS.</div>
              )}
            </div>


          <section className="portfolio-brief">
            <div className="portfolio-brief-heading"><h2>Portfolio</h2><button type="button" onClick={() => setTab("portfolio")}>View portfolio ↗</button></div>
            <strong className="portfolio-count">{portfolioIds.size}</strong>
            <span className="dim">Tracked securities</span>
            <p>Research and monitor your holdings in one place.</p>
            <button type="button" className="portfolio-open" onClick={() => setTab("portfolio")}>Open portfolio</button>
          </section>
        </aside>
      </div>

      <details className="research-drawer">
        <summary><span>Research assistant</span><span>Commands &amp; conversations <span aria-hidden="true">↑</span></span></summary>
        <div className="research-drawer-body">
          <AgentPanel onOpen={openChat} onAsk={askChat} />
          <div className="research-drawer-reference">            {/* Shortcuts */}
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

            <div className="ov-rail-box">
              <div className="ov-panel-label">Recent conversations</div>
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

</div>
        </div>
      </details>
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
