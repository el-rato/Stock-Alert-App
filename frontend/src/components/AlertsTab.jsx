import { useCallback, useEffect, useMemo, useState } from "react";
import {
  createPriceAlert,
  deletePriceAlert,
  notificationsScan,
  priceAlerts,
  updatePriceAlert,
} from "../api.js";
import { useApp } from "../App.jsx";
import { StatusIndicator } from "./ui.jsx";

function money(value) {
  if (value == null) return "—";
  return Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function distanceLabel(rule) {
  if (rule.current_price == null || rule.distance_pct == null) return "Waiting for quote";
  const pct = Math.abs(Number(rule.distance_pct) * 100);
  const reached = rule.direction === "above"
    ? Number(rule.current_price) >= Number(rule.target_price)
    : Number(rule.current_price) <= Number(rule.target_price);
  if (reached) return "Target reached";
  return `${pct.toFixed(pct < 1 ? 2 : 1)}% away`;
}

function stateOf(rule) {
  if (rule.triggered_at) return "triggered";
  if (rule.active) return "active";
  return "paused";
}

export default function AlertsTab() {
  const { market, markets, openDrawer, refreshToken } = useApp();
  const [rules, setRules] = useState([]);
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [form, setForm] = useState({
    market: market || "NYSE",
    ticker: "",
    direction: "above",
    target_price: "",
    note: "",
  });

  useEffect(() => {
    if (market) setForm((current) => ({ ...current, market }));
  }, [market]);

  const load = useCallback(() => {
    setLoading(true);
    return priceAlerts()
      .then(setRules)
      .catch((error) => setMessage(error.message || "Could not load price alerts"))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load, refreshToken]);

  const metrics = useMemo(() => ({
    active: rules.filter((rule) => rule.active).length,
    near: rules.filter((rule) => rule.active && rule.distance_pct != null && Math.abs(rule.distance_pct) <= 0.05).length,
    triggered: rules.filter((rule) => rule.triggered_at).length,
  }), [rules]);

  const submit = async (event) => {
    event.preventDefault();
    const ticker = form.ticker.trim().toUpperCase();
    const target = Number(form.target_price);
    if (!ticker || !Number.isFinite(target) || target <= 0) {
      setMessage("Enter a ticker and a valid target price.");
      return;
    }
    setSaving(true);
    setMessage("");
    try {
      await createPriceAlert({ ...form, ticker, target_price: target });
      setForm((current) => ({ ...current, ticker: "", target_price: "", note: "" }));
      setMessage(`${form.market}:${ticker} alert created.`);
      await load();
    } catch (error) {
      setMessage(error.message || "Could not create alert");
    } finally {
      setSaving(false);
    }
  };

  const toggle = async (rule) => {
    setMessage("");
    try {
      await updatePriceAlert(rule.id, !rule.active);
      await load();
    } catch (error) {
      setMessage(error.message || "Could not update alert");
    }
  };

  const remove = async (rule) => {
    setMessage("");
    try {
      await deletePriceAlert(rule.id);
      setRules((current) => current.filter((item) => item.id !== rule.id));
    } catch (error) {
      setMessage(error.message || "Could not delete alert");
    }
  };

  const checkNow = async () => {
    setChecking(true);
    setMessage("");
    try {
      const result = await notificationsScan();
      setMessage(result.count ? `${result.count} new market event${result.count === 1 ? "" : "s"} detected.` : "All alerts checked. No new triggers.");
      await load();
    } catch (error) {
      setMessage(error.message || "Could not check alerts");
    } finally {
      setChecking(false);
    }
  };

  const openRule = (rule) => openDrawer({
    type: "stock",
    v: { market: rule.market, ticker: rule.ticker, company: "", reason: ["PRICE ALERT"] },
  });

  return (
    <div className="alerts-page">
      <div className="page-heading">
        <div>
          <div className="page-eyebrow">PERSONAL MONITORING</div>
          <h1>Price Alerts</h1>
          <p>Set precise thresholds and let Mesh monitor the tape for you.</p>
        </div>
        <button className="button-secondary" onClick={checkNow} disabled={checking}>
          <span className={checking ? "spin" : ""}>↻</span> {checking ? "CHECKING" : "CHECK NOW"}
        </button>
      </div>

      <div className="alert-metrics">
        <div className="alert-metric"><span>Active rules</span><strong>{metrics.active}</strong><StatusIndicator state="live" label="MONITORING" /></div>
        <div className="alert-metric"><span>Within 5%</span><strong>{metrics.near}</strong><small>NEAR TARGET</small></div>
        <div className="alert-metric"><span>Triggered</span><strong>{metrics.triggered}</strong><small>READY TO REVIEW</small></div>
      </div>

      <div className="alerts-layout">
        <section className="alert-builder panel-surface">
          <div className="panel-title"><span>CREATE ALERT</span><small>ONE-SHOT RULE</small></div>
          <form onSubmit={submit}>
            <label>
              <span>Security</span>
              <div className="security-fields">
                <select value={form.market} onChange={(e) => setForm({ ...form, market: e.target.value })}>
                  {(markets?.length ? markets : [{ code: "NYSE" }]).map((item) => (
                    <option key={item.code} value={item.code}>{item.code}</option>
                  ))}
                </select>
                <input value={form.ticker} onChange={(e) => setForm({ ...form, ticker: e.target.value.toUpperCase() })} placeholder="AAPL" autoComplete="off" />
              </div>
            </label>
            <label>
              <span>Trigger when price is</span>
              <div className="target-fields">
                <select value={form.direction} onChange={(e) => setForm({ ...form, direction: e.target.value })}>
                  <option value="above">At or above</option>
                  <option value="below">At or below</option>
                </select>
                <input type="number" min="0.0001" step="any" value={form.target_price} onChange={(e) => setForm({ ...form, target_price: e.target.value })} placeholder="Target price" />
              </div>
            </label>
            <label>
              <span>Note <em>optional</em></span>
              <textarea maxLength="240" value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} placeholder="Why this level matters…" rows="3" />
            </label>
            <button className="button-primary alert-submit" disabled={saving}>{saving ? "CREATING…" : "CREATE PRICE ALERT"}</button>
            <p className="form-help">Rules trigger once, appear in Notifications, and can be re-armed at any time.</p>
          </form>
        </section>

        <section className="alert-rules panel-surface">
          <div className="panel-title">
            <span>YOUR RULES</span>
            <small>{rules.length} TOTAL</small>
          </div>
          {message && <div className="inline-message">{message}</div>}
          {loading ? (
            <div className="alert-empty">Loading alert rules…</div>
          ) : rules.length === 0 ? (
            <div className="alert-empty">
              <span className="alert-empty-icon">◎</span>
              <strong>No price alerts yet</strong>
              <p>Create your first rule to start monitoring a key price level.</p>
            </div>
          ) : (
            <div className="rules-list">
              {rules.map((rule) => {
                const state = stateOf(rule);
                return (
                  <article className={`price-rule ${state}`} key={rule.id}>
                    <button className="rule-security" onClick={() => openRule(rule)}>
                      <span className="rule-avatar">{rule.ticker.slice(0, 2)}</span>
                      <span><strong>{rule.ticker}</strong><small>{rule.market}</small></span>
                    </button>
                    <div className="rule-condition">
                      <small>TRIGGER {rule.direction.toUpperCase()}</small>
                      <strong>{money(rule.target_price)}</strong>
                    </div>
                    <div className="rule-current">
                      <small>LAST PRICE</small>
                      <strong>{money(rule.current_price)}</strong>
                    </div>
                    <div className="rule-distance">
                      <span className={`rule-status ${state}`}>{state}</span>
                      <small>{distanceLabel(rule)}</small>
                    </div>
                    {rule.note && <p className="rule-note">{rule.note}</p>}
                    <div className="rule-actions">
                      <button className="icon-button" onClick={() => toggle(rule)} title={rule.active ? "Pause alert" : "Re-arm alert"}>{rule.active ? "Ⅱ" : "▶"}</button>
                      <button className="icon-button danger" onClick={() => remove(rule)} title="Delete alert">×</button>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
