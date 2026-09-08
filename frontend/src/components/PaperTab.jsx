import { useCallback, useEffect, useMemo, useState } from "react";
import {
  paperPortfolios,
  paperCreatePortfolio,
  paperDeletePortfolio,
  paperResetPortfolio,
  paperPortfolio,
  paperOrders,
  paperPositions,
  paperCancelOrder,
  paperTrades,
  paperStats,
  paperRisk,
  paperLeaderboard,
  paperEquity,
  paperEndSession,
  paperSettle,
  paperDecisions,
  paperPerformance,
  paperEvaluate,
} from "../api.js";
import { useApp } from "../App.jsx";
import SecurityLink from "./SecurityLink.jsx";

function money(n, digits = 2) {
  const v = Number(n || 0);
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

function num(v, d = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
}

function Sparkline({ points }) {
  const vals = points.map((p) => p.equity);
  if (vals.length < 2) return <div className="empty" style={{ padding: 16 }}>NO EQUITY DATA YET.</div>;
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const range = max - min || 1;
  const w = 600;
  const h = 90;
  const coords = vals
    .map((v, i) => {
      const x = (i / (vals.length - 1)) * w;
      const y = h - ((v - min) / range) * h;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const last = vals[vals.length - 1];
  return (
    <div className="paper-equity">
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style={{ width: "100%", height: 90 }}>
        <polyline points={coords} fill="none" stroke={last >= vals[0] ? "var(--bull)" : "var(--bear)"} strokeWidth="1.5" />
      </svg>
      <div className="row"><span className="label">LATEST</span><span className="value">{money(last)}</span></div>
    </div>
  );
}

const STATUS_CLASS = {
  pending: "neutral", partial: "neutral", filled: "up", cancelled: "dim", rejected: "down",
};

export default function PaperTab() {
  const { refreshToken, openPaperTicket } = useApp();
  const [portfolios, setPortfolios] = useState([]);
  const [activeId, setActiveId] = useState("");
  const [pf, setPf] = useState(null);
  const [orders, setOrders] = useState([]);
  const [trades, setTrades] = useState([]);
  const [stats, setStats] = useState(null);
  const [risk, setRisk] = useState(null);
  const [board, setBoard] = useState(null);
  const [equity, setEquity] = useState([]);
  const [decisions, setDecisions] = useState([]);
  const [perf, setPerf] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [newName, setNewName] = useState("");
  const [newBalance, setNewBalance] = useState("100000");

  const load = useCallback(() => {
    setError("");
    paperPortfolios()
      .then((ps) => {
        setPortfolios(ps || []);
        const active = (ps && ps[0] && ps[0].id) || "";
        setActiveId((cur) => cur || active);
      })
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const reloadPortfolio = useCallback(() => {
    if (!activeId) return;
    Promise.all([
      paperPortfolio(activeId),
      paperOrders(activeId),
      paperPositions(activeId),
      paperTrades(activeId),
      paperStats(activeId),
      paperRisk(activeId),
      paperLeaderboard(activeId),
      paperEquity(activeId),
      paperDecisions(),
      paperPerformance(),
    ])
      .then(([p, o, pos, tr, st, rk, bd, eq, dec, perf2]) => {
        setPf(p); setOrders(o || []); setTrades(tr || []); setStats(st);
        setRisk(rk); setBoard(bd); setEquity(eq || []); setDecisions(dec || []); setPerf(perf2);
      })
      .catch((e) => setError(e.message));
  }, [activeId]);

  useEffect(() => {
    reloadPortfolio();
    const t = setInterval(reloadPortfolio, 30000);
    return () => clearInterval(t);
  }, [reloadPortfolio]);

  useEffect(() => {
    if (refreshToken) reloadPortfolio();
  }, [refreshToken, reloadPortfolio]);

  const decById = useMemo(() => {
    const m = {};
    for (const d of decisions || []) m[d.decision_id] = d;
    return m;
  }, [decisions]);

  const entryConviction = useMemo(() => {
    const map = {};
    for (const t of trades || []) {
      const key = `${t.market}:${t.ticker}`;
      if (!t.decision_id || map[key] != null) continue;
      const snap = decById[t.decision_id];
      if (snap) map[key] = snap.conviction;
    }
    return map;
  }, [trades, decById]);

  function createPortfolio() {
    const name = newName.trim();
    if (!name) return;
    setBusy(true);
    paperCreatePortfolio({ name, balance: num(newBalance, 100000) })
      .then((p) => {
        setNewName("");
        load();
        setActiveId(p.id);
      })
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false));
  }

  function removePortfolio() {
    if (!activeId) return;
    setBusy(true);
    paperDeletePortfolio(activeId)
      .then(() => {
        setActiveId("");
        load();
      })
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false));
  }

  function resetPortfolio() {
    if (!activeId) return;
    setBusy(true);
    paperResetPortfolio(activeId)
      .then(() => reloadPortfolio())
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false));
  }

  function endSession() {
    if (!activeId) return;
    setBusy(true);
    paperEndSession(activeId)
      .then(() => reloadPortfolio())
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false));
  }

  function settleIntraday() {
    setBusy(true);
    paperSettle(activeId)
      .then(() => reloadPortfolio())
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false));
  }

  function cancelOrder(orderId) {
    paperCancelOrder(orderId).then(reloadPortfolio).catch((e) => setError(e.message));
  }

  if (error && !pf && !portfolios.length) return <div className="error">ERROR: {error}</div>;
  if (!portfolios.length) {
    return (
      <div>
        <div className="empty" style={{ padding: 20 }}>NO PAPER PORTFOLIOS YET — CREATE ONE TO START SIMULATING.</div>
        <div className="controls" style={{ gap: 8 }}>
          <div className="field"><label>NAME</label>
            <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="e.g. Aggressive" />
          </div>
          <div className="field"><label>BALANCE</label>
            <input type="number" min="1" value={newBalance} onChange={(e) => setNewBalance(e.target.value)} />
          </div>
          <button className="primary" disabled={busy || !newName.trim()} onClick={createPortfolio}>+ CREATE PORTFOLIO</button>
        </div>
      </div>
    );
  }

  const openT = (p, side) =>
    openPaperTicket({ market: p.market, ticker: p.ticker, company: p.ticker, action: side, portfolio_id: activeId });

  return (
    <>
      {error && <div className="scan-warning">⚠ {error}</div>}

      <div className="controls" style={{ gap: 8, flexWrap: "wrap" }}>
        <div className="field"><label>PORTFOLIO</label>
          <select value={activeId} onChange={(e) => setActiveId(e.target.value)}>
            {portfolios.map((p) => <option key={p.id} value={p.id}>{p.name} · {p.currency}</option>)}
          </select>
        </div>
        <div className="field"><label>NEW</label>
          <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="name" />
        </div>
        <div className="field"><label>BALANCE</label>
          <input type="number" min="1" value={newBalance} onChange={(e) => setNewBalance(e.target.value)} />
        </div>
        <button className="primary" disabled={busy || !newName.trim()} onClick={createPortfolio}>+ CREATE</button>
        <button className="ghost" disabled={busy || !activeId} onClick={resetPortfolio}>RESET</button>
        <button className="ghost" disabled={busy || !activeId} onClick={removePortfolio}>DELETE</button>
        <button className="ghost" disabled={busy || !activeId} onClick={settleIntraday}>SETTLE INTRADAY</button>
      </div>

      {pf && (
        <div className="landing-stats">
          <div className="landing-stat"><div className="k">PORTFOLIO</div><div className="v">{pf.name}</div></div>
          <div className="landing-stat"><div className="k">EQUITY</div><div className="v" style={{ color: "var(--amber)" }}>{money(pf.equity)}</div></div>
          <div className="landing-stat"><div className="k">CASH</div><div className="v">{money(pf.cash)}</div></div>
          <div className="landing-stat"><div className="k">TOTAL P&L</div><div className="v" style={{ color: pf.total_pnl >= 0 ? "var(--bull)" : "var(--bear)" }}>{money(pf.total_pnl)} ({num(pf.day_pct).toFixed(2)}%)</div></div>
          <div className="landing-stat"><div className="k">GROSS / NET</div><div className="v">{money(pf.gross_exposure)} / {money(pf.net_exposure)}</div></div>
          <div className="landing-stat"><div className="k">POSITIONS</div><div className="v">{pf.open_positions}</div></div>
          <div className="landing-stat"><div className="k">LEVERAGE</div><div className="v">{num(pf.leverage).toFixed(1)}x</div></div>
          <div className="landing-stat"><div className="k">REALIZED</div><div className="v" style={{ color: pf.realized_pnl >= 0 ? "var(--bull)" : "var(--bear)" }}>{money(pf.realized_pnl)}</div></div>
        </div>
      )}

      <div className="landing-h" style={{ marginTop: 14 }}>POSITIONS
        <button className="ghost" style={{ marginLeft: 12, padding: "2px 8px", fontSize: 10 }} disabled={busy || !activeId} onClick={endSession}>END SESSION (LIQUIDATE ALL)</button>
      </div>
      {!pf?.positions?.length ? (
        <div className="empty">NO OPEN POSITIONS — USE BUY/SELL ON ANY STOCK VIEW TO OPEN A SIMULATED TRADE.</div>
      ) : (
        <div className="paper-table">
          <div className="paper-row paper-row-head">
            <span>SECURITY</span><span>SIDE</span><span>QTY</span><span>ENTRY</span><span>CURRENT</span><span>MV</span><span>UNREALIZED</span><span>MARGIN</span><span>PRODUCT</span><span>ACTIONS</span>
          </div>
          {pf.positions.map((p) => {
            const conv = entryConviction[`${p.market}:${p.ticker}`];
            return (
              <div className="paper-row" key={`${p.market}:${p.ticker}:${p.side}`}>
                <span className="sym"><SecurityLink market={p.market} ticker={p.ticker}>{p.ticker}</SecurityLink></span>
                <span className={p.side === "long" ? "up" : "down"}>{p.side.toUpperCase()}</span>
                <span>{p.quantity}</span>
                <span>{num(p.entry_price).toFixed(4)}</span>
                <span>{num(p.current_price).toFixed(4)}</span>
                <span>{money(p.value)}</span>
                <span style={{ color: p.unrealized_pnl >= 0 ? "var(--bull)" : "var(--bear)" }}>{money(p.unrealized_pnl)}</span>
                <span>{money(p.held_margin)}</span>
                <span className="dim">{p.product || "MIS"}{conv != null ? ` · ${Math.round(conv * 100)}%` : ""}</span>
                <span className="paper-actions">
                  <button className="paper-buy" onClick={() => openT(p, "buy")}>BUY</button>
                  <button className="paper-short" onClick={() => openT(p, "sell")}>SELL</button>
                </span>
              </div>
            );
          })}
        </div>
      )}

      <div className="landing-h" style={{ marginTop: 14 }}>ORDER BOOK</div>
      {!orders.length ? (
        <div className="empty">NO ORDERS YET.</div>
      ) : (
        <div className="paper-table">
          <div className="paper-row paper-row-head"><span>TIME</span><span>SIDE</span><span>TYPE</span><span>SECURITY</span><span>QTY</span><span>FILLED</span><span>AVG</span><span>STATUS</span><span></span></div>
          {orders.slice(0, 40).map((o) => (
            <div className="paper-row" key={o.id}>
              <span>{String(o.created_at).slice(11, 19)}</span>
              <span className={o.side === "buy" ? "up" : "down"}>{o.side.toUpperCase()}</span>
              <span className="dim">{o.order_type.toUpperCase()}</span>
              <span><SecurityLink market={o.market} ticker={o.ticker}>{o.ticker}</SecurityLink></span>
              <span>{o.quantity}</span>
              <span>{num(o.filled_qty)}</span>
              <span>{o.avg_price != null ? num(o.avg_price).toFixed(4) : "—"}</span>
              <span className={STATUS_CLASS[o.status] || "dim"}>{o.status.toUpperCase()}{o.reduce_only ? " · RO" : ""}</span>
              <span>{o.status === "pending" || o.status === "partial" ? <button className="ghost" style={{ padding: "2px 6px", fontSize: 10 }} onClick={() => cancelOrder(o.id)}>CANCEL</button> : ""}</span>
            </div>
          ))}
        </div>
      )}

      <div className="landing-h" style={{ marginTop: 14 }}>EQUITY</div>
      <Sparkline points={equity} />

      <div className="landing-h" style={{ marginTop: 14 }}>TRADE HISTORY</div>
      {!trades.length ? (
        <div className="empty">NO SIMULATED TRADES YET.</div>
      ) : (
        <div className="paper-table">
          <div className="paper-row paper-row-head"><span>TIME</span><span>SIDE</span><span>SECURITY</span><span>QTY @ PRICE</span><span>FEE</span><span>P&L</span></div>
          {trades.slice(0, 40).map((t) => (
            <div className="paper-row" key={t.id}>
              <span>{String(t.timestamp).slice(11, 19)}</span>
              <span className={t.side === "buy" ? "up" : "down"}>{t.side.toUpperCase()}</span>
              <span><SecurityLink market={t.market} ticker={t.ticker}>{t.ticker}</SecurityLink></span>
              <span>{t.quantity} @ {num(t.price).toFixed(4)}</span>
              <span className="dim">{num(t.fee).toFixed(2)}</span>
              <span style={{ color: t.pnl >= 0 ? "var(--bull)" : "var(--bear)" }}>{money(t.pnl)}</span>
            </div>
          ))}
        </div>
      )}

      <div className="landing-h" style={{ marginTop: 14 }}>TRADE STATS (REALIZED)</div>
      {!stats || stats.total_trades < 3 ? (
        <div className="empty" style={{ padding: 20 }}>TOO FEW CLOSED TRADES ({stats?.total_trades || 0}) — STATS SHOWN AFTER 3+.</div>
      ) : (
        <div className="landing-stats">
          <div className="landing-stat"><div className="k">TRADES</div><div className="v">{stats.total_trades}</div></div>
          <div className="landing-stat"><div className="k">WIN RATE</div><div className="v">{stats.win_rate != null ? (stats.win_rate * 100).toFixed(1) + "%" : "—"}</div></div>
          <div className="landing-stat"><div className="k">PROFIT FACTOR</div><div className="v">{stats.profit_factor != null && stats.profit_factor !== Infinity ? stats.profit_factor.toFixed(2) : "—"}</div></div>
          <div className="landing-stat"><div className="k">GROSS P/L</div><div className="v">{money(stats.gross_profit)} / {money(stats.gross_loss)}</div></div>
          <div className="landing-stat"><div className="k">AVG WIN / LOSS</div><div className="v">{money(stats.avg_win)} / {money(stats.avg_loss)}</div></div>
          <div className="landing-stat"><div className="k">LARGEST W/L</div><div className="v">{money(stats.largest_win)} / {money(stats.largest_loss)}</div></div>
          <div className="landing-stat"><div className="k">TOTAL FEES</div><div className="v">{money(stats.total_fees)}</div></div>
          <div className="landing-stat"><div className="k">TODAY P&L</div><div className="v" style={{ color: stats.today_pnl >= 0 ? "var(--bull)" : "var(--bear)" }}>{money(stats.today_pnl)}</div></div>
        </div>
      )}

      <div className="landing-h" style={{ marginTop: 14 }}>PORTFOLIO RISK</div>
      {risk?.warnings?.map((w, i) => <div className="scan-warning" key={i}>⚠ {w}</div>)}
      <div className="landing-stats">
        <div className="landing-stat"><div className="k">GROSS</div><div className="v">{money(risk?.gross_exposure)}</div></div>
        <div className="landing-stat"><div className="k">NET</div><div className="v">{money(risk?.net_exposure)}</div></div>
        <div className="landing-stat"><div className="k">LONG / SHORT</div><div className="v">{money(risk?.long_exposure)} / {money(risk?.short_exposure)}</div></div>
        <div className="landing-stat"><div className="k">CONCENTRATION</div><div className="v">{risk?.concentration != null ? (risk.concentration * 100).toFixed(0) + "%" : "—"}</div></div>
      </div>

      <div className="landing-h" style={{ marginTop: 14 }}>DECISION PERFORMANCE
        <button className="ghost" style={{ marginLeft: 12, padding: "2px 8px", fontSize: 10 }} onClick={() => paperEvaluate().then(reloadPortfolio)}>⟳ EVALUATE</button>
      </div>
      {perf && (
        <div className="landing-stats">
          <div className="landing-stat"><div className="k">DECISIONS</div><div className="v">{perf.decisions}</div></div>
          <div className="landing-stat"><div className="k">EVALUATED</div><div className="v">{perf.evaluated}</div></div>
          <div className="landing-stat"><div className="k">DIR ACC</div><div className="v">{perf.directional_accuracy != null ? (perf.directional_accuracy * 100).toFixed(1) + "%" : "N/A"}</div></div>
          <div className="landing-stat"><div className="k">RESEARCH CONF</div><div className="v">{perf.research_confidence_avg != null ? (perf.research_confidence_avg * 100).toFixed(0) + "%" : "N/A"}</div></div>
        </div>
      )}

      <div className="landing-h" style={{ marginTop: 14 }}>PAPER TRADING LEADERBOARD</div>
      <div className="paper-table">
        <div className="paper-row paper-row-head"><span>RANK</span><span>TRADER</span><span>EQUITY</span><span>RETURN</span><span>POS</span><span>TRADES</span></div>
        {(board?.rows || []).map((r) => (
          <div className="paper-row" key={r.name}>
            <span>{r.rank}</span>
            <span>{r.name}{r.is_demo ? <span className="badge neutral" style={{ marginLeft: 6, fontSize: 8 }}>DEMO</span> : ""}</span>
            <span>{money(r.equity)}</span>
            <span style={{ color: r.return >= 0 ? "var(--bull)" : "var(--bear)" }}>{r.return > 0 ? "+" : ""}{r.return}%</span>
            <span>{r.positions ?? "—"}</span>
            <span>{r.trades ?? "—"}</span>
          </div>
        ))}
      </div>
      {board?.demo_label && <div className="team-note">{board.demo_label}</div>}
      <div className="team-note" style={{ marginTop: 8 }}>PAPER TRADING — SIMULATION ONLY. NO REAL ORDERS, NO REAL MONEY. Leverage/margin/fees are configurable simulation assumptions.</div>
    </>
  );
}
