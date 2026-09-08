import { useEffect, useState } from "react";
import { paperQuote, paperPortfolio, paperPlaceOrder, paperDecisions } from "../api.js";
import { useApp } from "../App.jsx";
import SecurityLink from "./SecurityLink.jsx";

function num(v, d = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
}

const ORDER_TYPES = [
  { key: "market", label: "MARKET" },
  { key: "limit", label: "LIMIT" },
  { key: "stop", label: "STOP" },
  { key: "stop_limit", label: "STOP-LIMIT" },
];

function sideFromAction(action, position) {
  const a = String(action || "").toUpperCase();
  if (a === "BUY" || a === "COVER") return "buy";
  if (a === "SELL" || a === "SHORT") return "sell";
  if (a === "CLOSE" && position) return position.side === "long" ? "sell" : "buy";
  return "buy";
}

export default function PaperOrderPanel({ ticket, onClose }) {
  const { refreshAll } = useApp();
  const [quote, setQuote] = useState(null);
  const [pf, setPf] = useState(null);
  const [side, setSide] = useState("buy");
  const [orderType, setOrderType] = useState("market");
  const [qty, setQty] = useState("");
  const [price, setPrice] = useState("");
  const [stopPrice, setStopPrice] = useState("");
  const [reduceOnly, setReduceOnly] = useState(false);
  const [product, setProduct] = useState("MIS");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [decision, setDecision] = useState(ticket?.decision || null);

  const market = ticket?.market;
  const ticker = ticket?.ticker;
  const company = ticket?.company || ticker || "";
  const portfolioId = ticket?.portfolio_id || "";

  useEffect(() => {
    if (!market || !ticker) return;
    setError("");
    setSide(sideFromAction(ticket?.action, null));
    Promise.all([paperQuote(market, ticker), paperPortfolio(portfolioId)])
      .then(([q, p]) => {
        setQuote(q);
        setPf(p);
        const pos = (p.positions || []).find((x) => x.market === market && x.ticker === ticker);
        setSide(sideFromAction(ticket?.action, pos));
        const action = String(ticket?.action || "").toUpperCase();
        if (pos && (action === "CLOSE" || (action === "SELL" && pos.side === "long") || (action === "COVER" && pos.side === "short"))) {
          setReduceOnly(true);
        }
      })
      .catch((e) => setError(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [market, ticker, portfolioId]);

  useEffect(() => {
    if (!ticket?.decision || !market || !ticker) return;
    paperDecisions(market, ticker)
      .then((ds) => {
        if (ds && ds[0]) {
          try {
            const j = JSON.parse(ds[0].decision_json || "{}");
            setDecision({ decision_id: ds[0].decision_id, ...j });
          } catch (e) {
            /* ignore */
          }
        }
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [market, ticker]);

  if (!ticket) return null;

  const position = (pf?.positions || []).find((x) => x.market === market && x.ticker === ticker);
  const priceNum = quote?.price;
  const q = num(qty, 0);
  const refPrice = orderType === "limit" ? num(price, 0) : orderType === "stop" || orderType === "stop_limit" ? num(stopPrice, 0) : num(priceNum, 0);
  const estValue = priceNum != null ? q * priceNum : null;

  function confirm() {
    if (q <= 0) {
      setError("enter a valid quantity");
      return;
    }
    if (orderType === "limit" && num(price, 0) <= 0) {
      setError("limit order requires a price");
      return;
    }
    if ((orderType === "stop" || orderType === "stop_limit") && num(stopPrice, 0) <= 0) {
      setError("stop order requires a stop price");
      return;
    }
    setBusy(true);
    setError("");
    paperPlaceOrder({
      portfolio_id: portfolioId,
      market,
      ticker,
      side,
      order_type: orderType,
      quantity: q,
      price: orderType === "limit" || orderType === "stop_limit" ? num(price, 0) : null,
      stop_price: orderType === "stop" || orderType === "stop_limit" ? num(stopPrice, 0) : null,
      reduce_only: reduceOnly,
      product,
      exchange: market,
      decision_id: decision?.decision_id || "",
      reason: decision ? `Committee ${decision.verdict}` : "",
    })
      .then(() => {
        refreshAll();
        onClose();
      })
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false));
  }

  const isBuy = side === "buy";

  return (
    <>
      <div className="overlay open" onClick={onClose} />
      <aside className={`paper-order-panel ${isBuy ? "is-buy" : "is-sell"}`} role="dialog" aria-label="Paper order entry">
        <div className="paper-order-head">
          <span className={`paper-order-title ${isBuy ? "bull" : "bear"}`}>ORDER ENTRY</span>
          <button className="close" onClick={onClose} title="Close">✕</button>
        </div>

        <div className="paper-side-tabs">
          <button className={`side-tab ${isBuy ? "active buy" : ""}`} onClick={() => setSide("buy")}>BUY</button>
          <button className={`side-tab ${!isBuy ? "active sell" : ""}`} onClick={() => setSide("sell")}>SELL</button>
        </div>

        <div className="paper-order-sec">
          <SecurityLink market={market} ticker={ticker} className="symbol-lg">{ticker}</SecurityLink>
          <div className="dossier-company">{company} · {market}</div>
        </div>

        {error && <div className="scan-warning">⚠ {error}</div>}
        {!quote ? (
          <div className="empty" style={{ padding: 24 }}>LOADING QUOTE…</div>
        ) : quote.status === "no_data" ? (
          <div className="empty" style={{ padding: 24 }}>NO_DATA — no valid market price for {market}:{ticker}.</div>
        ) : (
          <>
            <div className="paper-info-row"><span>PRICE</span><strong>{num(priceNum).toFixed(4)}</strong></div>
            <div className="paper-info-row">
              <span>POSITION</span>
              <strong>{position ? `${position.side.toUpperCase()} ${position.quantity} @ ${num(position.entry_price).toFixed(4)}` : "NONE"}</strong>
            </div>
            <div className="paper-info-row"><span>CASH</span><strong>{num(pf?.cash).toFixed(2)}</strong></div>

            <div className="field">
              <label>ORDER TYPE</label>
              <select value={orderType} onChange={(e) => setOrderType(e.target.value)}>
                {ORDER_TYPES.map((t) => <option key={t.key} value={t.key}>{t.label}</option>)}
              </select>
            </div>

            <div className="field">
              <label>QUANTITY</label>
              <input type="number" min="1" value={qty} onChange={(e) => setQty(e.target.value)} placeholder="0" />
            </div>

            {(orderType === "limit" || orderType === "stop_limit") && (
              <div className="field">
                <label>LIMIT PRICE</label>
                <input type="number" min="0" step="0.01" value={price} onChange={(e) => setPrice(e.target.value)} placeholder="0.00" />
              </div>
            )}
            {(orderType === "stop" || orderType === "stop_limit") && (
              <div className="field">
                <label>STOP PRICE</label>
                <input type="number" min="0" step="0.01" value={stopPrice} onChange={(e) => setStopPrice(e.target.value)} placeholder="0.00" />
              </div>
            )}

            <div className="field">
              <label>PRODUCT</label>
              <select value={product} onChange={(e) => setProduct(e.target.value)} title="MIS = intraday (auto-squared at session close); CNC = delivery (carries forward)">
                <option value="MIS">MIS · INTRADAY</option>
                <option value="CNC">CNC · DELIVERY</option>
              </select>
            </div>

            <label className="paper-check" title="Only reduce an existing position; never open new exposure">
              <input type="checkbox" checked={reduceOnly} onChange={(e) => setReduceOnly(e.target.checked)} /> REDUCE ONLY
            </label>

            <div className="paper-info-row"><span>EST. VALUE</span><strong>{estValue != null ? estValue.toFixed(2) : "—"}</strong></div>
            <div className="paper-info-row"><span>REF (MARGIN)</span><strong>{refPrice > 0 ? refPrice.toFixed(4) : "—"}</strong></div>

            {decision && (
              <div className="paper-committee">
                <span className={`badge ${decision.verdict === "BULL" ? "bull" : decision.verdict === "BEAR" ? "bear" : "neutral"}`}>{decision.verdict}</span>
                <span>CONVICTION {decision.conviction != null ? Math.round(decision.conviction * 100) : "—"}%</span>
                {decision.thesis && <span className="paper-thesis">{decision.thesis}</span>}
              </div>
            )}

            <button className={`paper-submit ${isBuy ? "buy" : "sell"}`} disabled={busy} onClick={confirm}>
              {busy ? "SUBMITTING…" : `${isBuy ? "OPEN BUY" : "OPEN SELL"} ORDER`}
            </button>
            <div className="team-note">SIMULATION ONLY — NO REAL ORDERS, NO REAL MONEY.</div>
          </>
        )}
      </aside>
    </>
  );
}
