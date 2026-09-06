// Base URL for the backend API. Empty = same origin (recommended: serve the
// built frontend from the API or reverse-proxy /api to it). For a CDN/static
// deployment on another domain, set VITE_API_BASE_URL at build time, e.g.
//   VITE_API_BASE_URL=https://api.example.com npm run build
// (no trailing slash; never put provider secrets in frontend env vars).
export const API_BASE = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/+$/, "");

export function apiUrl(path) {
  if (/^https?:\/\//i.test(path)) return path;
  return `${API_BASE}${path}`;
}

export async function fetchJSON(url, opts) {
  const r = await fetch(apiUrl(url), opts);
  if (!r.ok) {
    let detail = "HTTP " + r.status;
    try {
      const j = await r.json();
      if (j.detail) detail = j.detail;
    } catch (e) {
      /* ignore */
    }
    throw new Error(detail);
  }
  return r.json();
}

export const CHART_RANGES = ["1d", "1w", "1mo", "3mo", "6mo", "1y"];

export function rangeLabel(r) {
  if (r === "all") return "ALL";
  return r.toUpperCase();
}

export async function lstmBatchPredict(symbols, period = "2y", window = 30) {
  return fetchJSON(`/api/lstm/batch-predict?symbols=${encodeURIComponent(symbols.join(","))}&period=${period}&window=${window}`);
}

export async function lstmTrain(symbol, period = "2y", window = 30, epochs = 25, batch_size = 32, lr = 1e-3) {
  return fetchJSON(`/api/lstm/train?symbol=${encodeURIComponent(symbol)}&period=${period}&window=${window}&epochs=${epochs}&batch_size=${batch_size}&lr=${lr}`);
}

export async function dossier(params) {
  const qs = new URLSearchParams();
  if (params.symbol) qs.set("symbol", params.symbol);
  if (params.market) qs.set("market", params.market);
  if (params.ticker) qs.set("ticker", params.ticker);
  if (params.fresh) qs.set("fresh", "true");
  return fetchJSON(`/api/dossier?${qs.toString()}`);
}

export async function scanner(params = {}) {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") qs.set(k, v);
  });
  return fetchJSON(`/api/scanner?${qs.toString()}`);
}

export async function paperPortfolios() {
  return fetchJSON("/api/paper/portfolios");
}

export async function paperCreatePortfolio(body) {
  return fetchJSON("/api/paper/portfolios", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function paperDeletePortfolio(portfolioId) {
  return fetchJSON(`/api/paper/portfolios/${encodeURIComponent(portfolioId)}`, { method: "DELETE" });
}

export async function paperResetPortfolio(portfolioId) {
  return fetchJSON(`/api/paper/portfolios/${encodeURIComponent(portfolioId)}/reset`, { method: "POST" });
}

export async function paperSetBalance(portfolioId, balance) {
  return fetchJSON(`/api/paper/portfolios/${encodeURIComponent(portfolioId)}/balance`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ balance }),
  });
}

export async function paperSetMarketHours(portfolioId, enforce) {
  return fetchJSON(`/api/paper/portfolios/${encodeURIComponent(portfolioId)}/market-hours`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enforce }),
  });
}

export async function paperPortfolio(portfolioId = "") {
  const qs = new URLSearchParams();
  if (portfolioId) qs.set("portfolio_id", portfolioId);
  return fetchJSON(`/api/paper/portfolio?${qs.toString()}`);
}

export async function paperQuote(market, ticker) {
  return fetchJSON(`/api/paper/quote?market=${encodeURIComponent(market)}&ticker=${encodeURIComponent(ticker)}`);
}

export async function paperPlaceOrder(body) {
  return fetchJSON("/api/paper/order", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function paperCancelOrder(orderId) {
  return fetchJSON(`/api/paper/orders/${encodeURIComponent(orderId)}/cancel`, { method: "POST" });
}

export async function paperOrders(portfolioId = "", status = "") {
  const qs = new URLSearchParams();
  if (portfolioId) qs.set("portfolio_id", portfolioId);
  if (status) qs.set("status", status);
  return fetchJSON(`/api/paper/orders?${qs.toString()}`);
}

export async function paperPositions(portfolioId = "") {
  const qs = new URLSearchParams();
  if (portfolioId) qs.set("portfolio_id", portfolioId);
  return fetchJSON(`/api/paper/positions?${qs.toString()}`);
}

export async function paperConvertPosition(positionId, product) {
  return fetchJSON(`/api/paper/positions/${encodeURIComponent(positionId)}/convert`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ product }),
  });
}

export async function paperTrades(portfolioId = "", limit = 100) {
  const qs = new URLSearchParams();
  if (portfolioId) qs.set("portfolio_id", portfolioId);
  qs.set("limit", String(limit));
  return fetchJSON(`/api/paper/trades?${qs.toString()}`);
}

export async function paperStats(portfolioId = "") {
  const qs = new URLSearchParams();
  if (portfolioId) qs.set("portfolio_id", portfolioId);
  return fetchJSON(`/api/paper/stats?${qs.toString()}`);
}

export async function paperRisk(portfolioId = "") {
  const qs = new URLSearchParams();
  if (portfolioId) qs.set("portfolio_id", portfolioId);
  return fetchJSON(`/api/paper/risk?${qs.toString()}`);
}

export async function paperEquity(portfolioId = "") {
  const qs = new URLSearchParams();
  if (portfolioId) qs.set("portfolio_id", portfolioId);
  return fetchJSON(`/api/paper/equity?${qs.toString()}`);
}

export async function paperEndSession(portfolioId = "") {
  const qs = new URLSearchParams();
  if (portfolioId) qs.set("portfolio_id", portfolioId);
  return fetchJSON(`/api/paper/end-session?${qs.toString()}`, { method: "POST" });
}

export async function paperSettle(portfolioId = "") {
  const qs = new URLSearchParams();
  if (portfolioId) qs.set("portfolio_id", portfolioId);
  return fetchJSON(`/api/paper/settle?${qs.toString()}`, { method: "POST" });
}

export async function paperLeaderboard(portfolioId = "") {
  const qs = new URLSearchParams();
  if (portfolioId) qs.set("portfolio_id", portfolioId);
  return fetchJSON(`/api/paper/leaderboard?${qs.toString()}`);
}

export async function paperDecisions(market = "", ticker = "") {
  const qs = new URLSearchParams();
  if (market) qs.set("market", market);
  if (ticker) qs.set("ticker", ticker);
  return fetchJSON(`/api/paper/decisions?${qs.toString()}`);
}

export async function paperPerformance() {
  return fetchJSON("/api/paper/performance");
}

export async function paperEvaluate() {
  return fetchJSON("/api/paper/evaluate", { method: "POST" });
}

export async function simulate(params) {
  return fetchJSON("/api/simulate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
}

export async function notifications(limit = 50) {
  return fetchJSON(`/api/notifications?limit=${limit}`);
}

export async function notificationsScan() {
  return fetchJSON("/api/notifications/scan", { method: "POST" });
}

export async function events(limit = 40) {
  return fetchJSON(`/api/events?limit=${limit}`);
}

export async function newsFeed(limit = 100) {
  return fetchJSON(`/api/news/feed?limit=${limit}`);
}

export async function newsForTicker(market, ticker, { limit = 200, refresh = true } = {}) {
  const qs = new URLSearchParams();
  qs.set("market", market);
  qs.set("ticker", ticker);
  qs.set("limit", String(limit));
  if (refresh) qs.set("refresh", "true");
  return fetchJSON(`/api/news?${qs.toString()}`);
}

export async function agentChat(messages, market = "", mode = "AUTO", provider = "auto", model = "", search = "") {
  return fetchJSON("/api/agent/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages, market: market || null, mode, provider, model, search }),
  });
}

export async function agentConfig() {
  return fetchJSON("/api/agent/config");
}

export async function notificationsAck(keys) {
  return fetchJSON("/api/notifications/ack", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keys }),
  });
}

export async function priceAlerts() {
  return fetchJSON("/api/price-alerts");
}

export async function createPriceAlert(body) {
  return fetchJSON("/api/price-alerts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function updatePriceAlert(id, active) {
  return fetchJSON(`/api/price-alerts/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ active }),
  });
}

export async function deletePriceAlert(id) {
  return fetchJSON(`/api/price-alerts/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function tickerStrip() {
  return fetchJSON("/api/ticker-strip");
}

export async function watchlist() {
  return fetchJSON("/api/watchlist");
}

// ---- Agent Workflow screening (the strategy-discovery path, in-agent) ----
export async function agentWorkflow(prompt, market = null, limit = 30, criteria = null) {
  return fetchJSON("/api/agent/workflow", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt: prompt || "", market: market || null, limit, criteria }),
  });
}

// ---- Portfolio Groups ----
export async function portfolioGroups() {
  return fetchJSON("/api/portfolio/groups");
}

export async function createPortfolioGroup({
  name, description = "", source = "manual", strategy_id = null,
  strategy_name = null, members = [],
}) {
  return fetchJSON("/api/portfolio/groups", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, description, source, strategy_id, strategy_name, members }),
  });
}

export async function renamePortfolioGroup(groupId, name, description = null) {
  return fetchJSON(`/api/portfolio/groups/${encodeURIComponent(groupId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, description }),
  });
}

export async function deletePortfolioGroup(groupId) {
  return fetchJSON(`/api/portfolio/groups/${encodeURIComponent(groupId)}`, {
    method: "DELETE",
  });
}

export async function addToGroup(groupId, market, ticker) {
  return fetchJSON(`/api/portfolio/groups/${encodeURIComponent(groupId)}/members`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ market, ticker }),
  });
}

export async function removeFromGroup(groupId, market, ticker) {
  return fetchJSON(
    `/api/portfolio/groups/${encodeURIComponent(groupId)}/members?market=${encodeURIComponent(market)}&ticker=${encodeURIComponent(ticker)}`,
    { method: "DELETE" },
  );
}

export async function authRegister(email, password, username = "") {
  return fetchJSON("/api/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password, username }),
  });
}

export async function authLogin(email, password) {
  return fetchJSON("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
}

export async function authLogout() {
  return fetchJSON("/api/auth/logout", { method: "POST" });
}

export async function authMe() {
  return fetchJSON("/api/auth/me");
}

export async function screener(params = {}) {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") qs.set(k, v);
  });
  return fetchJSON(`/api/screener?${qs.toString()}`);
}
