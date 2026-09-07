import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  const [pendingRule, setPendingRule] = useState("");
  const [notice, setNotice] = useState(null);
  const [errors, setErrors] = useState({});
  const tickerRef = useRef(null);
  const targetRef = useRef(null);
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
      .catch((error) => setNotice({ tone: "error", text: error.message || "Could not load price alerts" }))
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
    const nextErrors = {};
    if (!ticker) nextErrors.ticker = "Enter a ticker symbol.";
    if (!Number.isFinite(target) || target <= 0) nextErrors.target_price = "Enter a target greater than zero.";
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length) {
      setNotice({ tone: "error", text: "Review the highlighted fields." });
      (nextErrors.ticker ? tickerRef : targetRef).current?.focus();
      return;
    }
    setSaving(true);
    setNotice(null);
    try {
      await createPriceAlert({ ...form, ticker, target_price: target });
      setForm((current) => ({ ...current, ticker: "", target_price: "", note: "" }));
      setErrors({});
      setNotice({ tone: "success", text: `${form.market}:${ticker} is now being monitored.` });
      await load();
    } catch (error) {
      setNotice({ tone: "error", text: error.message || "Could not create alert" });
    } finally {
      setSaving(false);
    }
  };

  const toggle = async (rule) => {
    setPendingRule(`toggle:${rule.id}`);
    setNotice(null);
    try {
      await updatePriceAlert(rule.id, !rule.active);
      await load();
    } catch (error) {
      setNotice({ tone: "error", text: error.message || "Could not update alert" });
    } finally {
      setPendingRule("");
    }
  };

  const remove = async (rule) => {
    setPendingRule(`delete:${rule.id}`);
    setNotice(null);
    try {
      await deletePriceAlert(rule.id);
      setRules((current) => current.filter((item) => item.id !== rule.id));
      setNotice({ tone: "success", text: `${rule.market}:${rule.ticker} alert deleted.` });
    } catch (error) {
      setNotice({ tone: "error", text: error.message || "Could not delete alert" });
    } finally {
      setPendingRule("");
    }
  };

  const checkNow = async () => {
    setChecking(true);
    setNotice(null);
    try {
      const result = await notificationsScan();
      setNotice({
        tone: "success",
        text: result.count ? `${result.count} new market event${result.count === 1 ? "" : "s"} detected.` : "Check complete. No new triggers.",
      });
      await load();
    } catch (error) {
      setNotice({ tone: "error", text: error.message || "Could not check alerts" });
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
          <h1>Price Alerts</h1>
          <p>Track the price levels that would change your decision.</p>
        </div>
        <button className="button-secondary" onClick={checkNow} disabled={checking}>
          <span className={checking ? "spin" : ""} aria-hidden="true">↻</span> {checking ? "Checking…" : "Check now"}
        </button>
      </div>

      <div className="alert-metrics" aria-label="Alert monitoring summary">
        <div className="alert-metric"><span>Active rules</span><strong>{metrics.active}</strong><StatusIndicator state="live" label="Monitoring" /></div>
        <div className="alert-metric"><span>Within 5%</span><strong>{metrics.near}</strong><small>Near target</small></div>
        <div className="alert-metric"><span>Triggered</span><strong>{metrics.triggered}</strong><small>Ready to review</small></div>
      </div>

      <div className="alerts-layout">
        <section className="alert-builder panel-surface">
          <div className="panel-title"><span>Create alert</span><small>One-shot rule</small></div>
          <form onSubmit={submit} noValidate aria-describedby="alert-form-help">
            <fieldset>
              <legend>Security</legend>
              <div className="alert-form-row security-fields">
                <label htmlFor="alert-market">
                  <span>Market</span>
                  <select id="alert-market" value={form.market} onChange={(e) => setForm({ ...form, market: e.target.value })}>
                    {(markets?.length ? markets : [{ code: "NYSE" }]).map((item) => (
                      <option key={item.code} value={item.code}>{item.code}</option>
                    ))}
                  </select>
                </label>
                <label htmlFor="alert-ticker">
                  <span>Ticker</span>
                  <input
                    id="alert-ticker"
                    ref={tickerRef}
                    value={form.ticker}
                    onChange={(e) => {
                      setForm({ ...form, ticker: e.target.value.toUpperCase() });
                      if (errors.ticker) setErrors((current) => ({ ...current, ticker: "" }));
                    }}
                    placeholder="AAPL"
                    autoComplete="off"
                    aria-invalid={Boolean(errors.ticker)}
                    aria-describedby={errors.ticker ? "alert-ticker-error" : undefined}
                  />
                  {errors.ticker && <small className="field-error" id="alert-ticker-error">{errors.ticker}</small>}
                </label>
              </div>
            </fieldset>
            <fieldset>
              <legend>Threshold</legend>
              <div className="alert-form-row target-fields">
                <label htmlFor="alert-direction">
                  <span>Condition</span>
                  <select id="alert-direction" value={form.direction} onChange={(e) => setForm({ ...form, direction: e.target.value })}>
                    <option value="above">At or above</option>
                    <option value="below">At or below</option>
                  </select>
                </label>
                <label htmlFor="alert-target">
                  <span>Target price</span>
                  <input
                    id="alert-target"
                    ref={targetRef}
                    type="number"
                    min="0.0001"
                    step="any"
                    inputMode="decimal"
                    value={form.target_price}
                    onChange={(e) => {
                      setForm({ ...form, target_price: e.target.value });
                      if (errors.target_price) setErrors((current) => ({ ...current, target_price: "" }));
                    }}
                    placeholder="185.00"
                    aria-invalid={Boolean(errors.target_price)}
                    aria-describedby={errors.target_price ? "alert-target-error" : undefined}
                  />
                  {errors.target_price && <small className="field-error" id="alert-target-error">{errors.target_price}</small>}
                </label>
              </div>
            </fieldset>
            <label htmlFor="alert-note">
              <span>Note <em>optional</em></span>
              <textarea id="alert-note" maxLength="240" value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} placeholder="Why this level matters…" rows="3" />
            </label>
            <button className="button-primary alert-submit" disabled={saving}>{saving ? "Creating…" : "Create price alert"}</button>
            <p className="form-help" id="alert-form-help">Each rule triggers once, appears in Notifications, and can be re-armed.</p>
          </form>
        </section>

        <section className="alert-rules panel-surface" aria-busy={loading}>
          <div className="panel-title">
            <span>Your rules</span>
            <small>{rules.length} total</small>
          </div>
          {notice && (
            <div className={`inline-message ${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"} aria-live="polite">
              {notice.text}
            </div>
          )}
          {loading ? (
            <div className="alert-loading" role="status" aria-live="polite">
              <span className="sr-only">Loading alert rules…</span>
              {[0, 1, 2].map((item) => <span className="alert-skeleton-row" aria-hidden="true" key={item} />)}
            </div>
          ) : rules.length === 0 ? (
            <div className="alert-empty">
              <span className="alert-empty-icon" aria-hidden="true">◎</span>
              <strong>No price alerts yet</strong>
              <p>Create your first rule to start monitoring a key price level.</p>
              <button className="button-secondary" onClick={() => tickerRef.current?.focus()}>Create an alert</button>
            </div>
          ) : (
            <div className="rules-list">
              {rules.map((rule) => {
                const state = stateOf(rule);
                return (
                  <article className={`price-rule ${state}`} key={rule.id} aria-labelledby={`alert-rule-${rule.id}`}>
                    <button className="rule-security" onClick={() => openRule(rule)} aria-label={`Open ${rule.market}:${rule.ticker} dossier`}>
                      <span className="rule-avatar">{rule.ticker.slice(0, 2)}</span>
                      <span><strong id={`alert-rule-${rule.id}`}>{rule.ticker}</strong><small>{rule.market}</small></span>
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
                      <button
                        className="icon-button"
                        onClick={() => toggle(rule)}
                        title={rule.active ? "Pause alert" : "Re-arm alert"}
                        aria-label={`${rule.active ? "Pause" : "Re-arm"} ${rule.ticker} alert`}
                        disabled={Boolean(pendingRule)}
                      >
                        {pendingRule === `toggle:${rule.id}` ? "…" : rule.active ? "Ⅱ" : "▶"}
                      </button>
                      <button
                        className="icon-button danger"
                        onClick={() => remove(rule)}
                        title="Delete alert"
                        aria-label={`Delete ${rule.ticker} alert`}
                        disabled={Boolean(pendingRule)}
                      >
                        {pendingRule === `delete:${rule.id}` ? "…" : "×"}
                      </button>
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
