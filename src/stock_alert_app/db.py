from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# --- News text cleaning -----------------------------------------------------
# RSS feeds frequently ship either raw HTML (Google News wraps the headline in
# <a>/<font> tags) or a junk "summary" that is just the headline repeated with
# the source name appended. We clean on read so the UI never shows markup or
# echoed-title noise, regardless of what was stored at ingest time.
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Aggregator boilerplate to strip from summaries (Hacker News-style links/points).
_BOILER_RE = re.compile(
    r"\b(?:Article URL|Comments URL|Comments|Points)\b\s*[:=]?\s*"
    r"(?:https?://\S+|\d+)?"
    r"|#\s*Comments\s*:\s*\d+"
    r"|\bPoints\s*:\s*\d+\b"
    r"|\bComments\s*:\s*\d+\b",
    re.IGNORECASE,
)


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    no_tags = _TAG_RE.sub("", value)
    decoded = unescape(no_tags)
    return _WS_RE.sub(" ", decoded).strip()


def _strip_source_suffix(value: str, source: str | None) -> str:
    """Remove a trailing ' <source>' / ' - <source>' tag from a headline."""
    s = _clean_text(source)
    if not s:
        return value
    for pat in (f" - {s}", f" — {s}", f" – {s}", f" · {s}", f" {s}"):
        if value.endswith(pat):
            return value[: -len(pat)].strip()
    return value


def _clean_headline(raw: str | None, source: str | None) -> str:
    """Clean a headline: strip HTML and any trailing source tag."""
    return _strip_source_suffix(_clean_text(raw), source)


def _clean_summary(raw: str | None, title_clean: str, source: str | None) -> str:
    """Return a meaningful summary, or '' when the feed supplied junk.

    Many RSS feeds ship either raw HTML or a "summary" that is just the
    headline with the source appended (e.g. "Nestle slides Thursday
    MarketWatch"). After stripping the source tag, if what remains equals the
    headline it carries no extra information, so we drop it. A genuine summary
    that continues the headline with a real sentence is kept.

    Some aggregators (Hacker News, etc.) ship pure boilerplate such as
    "Article URL: https://…  Comments URL: https://…  Points: 11  # Comments: 1".
    We strip those tokens too; if only boilerplate remains the summary is empty.
    """
    s = _strip_source_suffix(_clean_text(raw), source)
    if not s:
        return ""
    # Drop common aggregator boilerplate tokens (URLs, points, comment counts).
    s = _BOILER_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    if not s:
        return ""
    if title_clean and s.lower() == title_clean.lower():
        return ""
    return s

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL,
    UNIQUE(market, ticker, url)
);

CREATE TABLE IF NOT EXISTS sentiment_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    news_item_id INTEGER NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    score REAL NOT NULL,
    label TEXT NOT NULL,
    positive REAL NOT NULL DEFAULT 0,
    negative REAL NOT NULL DEFAULT 0,
    neutral REAL NOT NULL DEFAULT 0,
    scored_at TEXT NOT NULL,
    UNIQUE(news_item_id, model)
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    close REAL NOT NULL,
    open REAL NOT NULL DEFAULT 0,
    high REAL NOT NULL DEFAULT 0,
    low REAL NOT NULL DEFAULT 0,
    volume INTEGER NOT NULL DEFAULT 0,
    momentum_20 REAL NOT NULL DEFAULT 0,
    rsi_14 REAL NOT NULL DEFAULT 50,
    sma_50 REAL NOT NULL DEFAULT 0,
    UNIQUE(market, ticker, fetched_at)
);

CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    verdict TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    news_score REAL NOT NULL DEFAULT 0,
    price_score REAL NOT NULL DEFAULT 0,
    combined_score REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL,
    UNIQUE(market, ticker, decided_at)
);

CREATE TABLE IF NOT EXISTS watchlist (
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL,
    PRIMARY KEY (market, ticker)
);

CREATE TABLE IF NOT EXISTS agent_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    rationale TEXT NOT NULL DEFAULT '',
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS securities (
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    exchange TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'configured',
    data_status TEXT NOT NULL DEFAULT 'no_data',
    last_analysis_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (market, ticker)
);

-- Immutable decision snapshots (paper research engine). Append-only per
-- (market, ticker, decided_at); a new analysis creates a new snapshot.
CREATE TABLE IF NOT EXISTS decision_snapshots (
    decision_id TEXT PRIMARY KEY,
    security_id TEXT NOT NULL,
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    verdict TEXT NOT NULL,
    conviction REAL,
    reference_price REAL,
    research_confidence REAL,
    decision_json TEXT NOT NULL DEFAULT '',
    UNIQUE(market, ticker, decided_at)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_security ON decision_snapshots(market, ticker, decided_at);

-- Paper trading (simulation only — no real orders ever).
CREATE TABLE IF NOT EXISTS paper_portfolio (
    session_id TEXT PRIMARY KEY,
    starting_cash REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    opened_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT UNIQUE NOT NULL,
    session_id TEXT NOT NULL,
    security_id TEXT NOT NULL,
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0,
    executed_at TEXT NOT NULL,
    decision_id TEXT,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_orders_session ON paper_orders(session_id, executed_at);

-- Historical decision evaluation (post-decision prices only; no look-ahead).
CREATE TABLE IF NOT EXISTS decision_evaluations (
    decision_id TEXT PRIMARY KEY,
    reference_price REAL,
    p5 REAL, p15 REAL, p30 REAL, p60 REAL, close_price REAL,
    correct INTEGER,
    status TEXT NOT NULL DEFAULT 'no_data',
    evaluated_at TEXT NOT NULL
);

-- Equity curve points for the paper portfolio (simulation only).
CREATE TABLE IF NOT EXISTS paper_equity_points (
    session_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    equity REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equity_session ON paper_equity_points(session_id, recorded_at);

-- ============================================================================
-- Paper trading v2 — Fincept-style multi-portfolio engine (simulation only).
-- The legacy paper_portfolio / paper_orders tables above are retained but
-- unused; all new state lives in the pt_* tables. Idempotent migration creates
-- these on existing databases without touching legacy rows.
-- ============================================================================

CREATE TABLE IF NOT EXISTS pt_portfolios (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    initial_balance REAL NOT NULL,
    balance REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    leverage REAL NOT NULL DEFAULT 1.0,
    margin_mode TEXT NOT NULL DEFAULT 'cross',
    fee_rate REAL NOT NULL DEFAULT 0.001,
    exchange TEXT NOT NULL DEFAULT '',
    enforce_market_hours INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pt_portfolios_user ON pt_portfolios(user_id);

CREATE TABLE IF NOT EXISTS pt_orders (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    security_id TEXT NOT NULL DEFAULT '',
    market TEXT NOT NULL DEFAULT '',
    ticker TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL,              -- 'buy' | 'sell'
    order_type TEXT NOT NULL,        -- 'market' | 'limit' | 'stop' | 'stop_limit'
    quantity REAL NOT NULL,
    price REAL,                       -- limit price (nullable)
    stop_price REAL,                  -- stop trigger (nullable)
    filled_qty REAL NOT NULL DEFAULT 0,
    avg_price REAL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|partial|filled|cancelled|rejected
    reduce_only INTEGER NOT NULL DEFAULT 0,
    margin_blocked REAL NOT NULL DEFAULT 0,
    product TEXT NOT NULL DEFAULT '',   -- 'MIS' | 'CNC' | '' (leverage hint)
    exchange TEXT NOT NULL DEFAULT '',
    decision_id TEXT,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    filled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pt_orders_portfolio ON pt_orders(portfolio_id, created_at);
CREATE INDEX IF NOT EXISTS idx_pt_orders_user ON pt_orders(user_id, created_at);

CREATE TABLE IF NOT EXISTS pt_positions (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    security_id TEXT NOT NULL DEFAULT '',
    market TEXT NOT NULL DEFAULT '',
    ticker TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL,              -- 'long' | 'short'
    quantity REAL NOT NULL,
    entry_price REAL NOT NULL,
    current_price REAL NOT NULL DEFAULT 0,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    leverage REAL NOT NULL DEFAULT 1.0,
    product TEXT NOT NULL DEFAULT 'MIS',
    held_margin REAL NOT NULL DEFAULT 0,
    opened_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pt_positions_portfolio ON pt_positions(portfolio_id);

CREATE TABLE IF NOT EXISTS pt_trades (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    security_id TEXT NOT NULL DEFAULT '',
    market TEXT NOT NULL DEFAULT '',
    ticker TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL,
    price REAL NOT NULL,
    quantity REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0,
    pnl REAL NOT NULL DEFAULT 0,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pt_trades_portfolio ON pt_trades(portfolio_id, timestamp);

CREATE TABLE IF NOT EXISTS pt_margin_blocks (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    security_id TEXT NOT NULL DEFAULT '',
    amount REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pt_margin_blocks_order ON pt_margin_blocks(order_id);

-- Backtest runs + immutable historical decision snapshots (rigorous mode only).
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    period TEXT NOT NULL,
    capital REAL NOT NULL,
    started_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    security_id TEXT NOT NULL,
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    ts TEXT NOT NULL,
    verdict TEXT NOT NULL,
    conviction REAL,
    reference_price REAL,
    signals_json TEXT NOT NULL DEFAULT '',
    forward_json TEXT NOT NULL DEFAULT '',
    correct INTEGER,
    UNIQUE(run_id, decision_id)
);

-- Historical replay runs + immutable chronological decision snapshots.
CREATE TABLE IF NOT EXISTS replay_runs (
    run_id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL,
    capital REAL NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replay_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    decision_id TEXT UNIQUE NOT NULL,
    security_id TEXT NOT NULL,
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,
    verdict TEXT NOT NULL,
    conviction REAL,
    reference_price REAL,
    execution_price REAL,
    quantity REAL,
    cash REAL,
    equity REAL,
    position_direction TEXT,
    position_qty REAL,
    reason TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '',
    UNIQUE(run_id, decision_id)
);
CREATE INDEX IF NOT EXISTS idx_replay_decisions_run ON replay_decisions(run_id, ts);

-- Terminal notifications (deterministic event keys, append-only).
CREATE TABLE IF NOT EXISTS notification_events (
    event_key TEXT PRIMARY KEY,
    severity TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    security_id TEXT NOT NULL DEFAULT '',
    market TEXT NOT NULL DEFAULT '',
    ticker TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    acked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notifications_created ON notification_events(created_at DESC);

-- Dedup registry for scanned events (market open / committee change / trades).
CREATE TABLE IF NOT EXISTS notification_processed (
    event_key TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL
);

-- User-defined one-shot price thresholds. Rules remain visible after they fire
-- so the alert history is auditable and a trader can re-arm them.
CREATE TABLE IF NOT EXISTS price_alert_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('above', 'below')),
    target_price REAL NOT NULL CHECK(target_price > 0),
    note TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    last_price REAL,
    triggered_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_alert_rules_active
    ON price_alert_rules(active, market, ticker);

CREATE TABLE IF NOT EXISTS fund_filings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cik TEXT NOT NULL,
    fund_name TEXT NOT NULL DEFAULT '',
    form TEXT NOT NULL DEFAULT '13F-HR',
    accession TEXT NOT NULL,
    filing_date TEXT NOT NULL,
    period_of_report TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL,
    UNIQUE(cik, accession)
);

CREATE TABLE IF NOT EXISTS fund_holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_filing_id INTEGER NOT NULL REFERENCES fund_filings(id) ON DELETE CASCADE,
    cusip TEXT NOT NULL DEFAULT '',
    issuer TEXT NOT NULL DEFAULT '',
    ticker TEXT NOT NULL DEFAULT '',
    value_thousands REAL NOT NULL DEFAULT 0,
    shares REAL NOT NULL DEFAULT 0,
    shares_type TEXT NOT NULL DEFAULT 'SH',
    put_call TEXT NOT NULL DEFAULT '',
    pct_portfolio REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_fund_holdings_filing ON fund_holdings(fund_filing_id);
CREATE INDEX IF NOT EXISTS idx_fund_holdings_ticker ON fund_holdings(ticker);

CREATE TABLE IF NOT EXISTS index_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL,
    close REAL NOT NULL DEFAULT 0,
    open REAL NOT NULL DEFAULT 0,
    high REAL NOT NULL DEFAULT 0,
    low REAL NOT NULL DEFAULT 0,
    volume INTEGER NOT NULL DEFAULT 0,
    change_pct REAL NOT NULL DEFAULT 0,
    UNIQUE(market, symbol, fetched_at)
);

CREATE INDEX IF NOT EXISTS idx_agent_recs_generated ON agent_recommendations(generated_at);
CREATE INDEX IF NOT EXISTS idx_news_ticker ON news_items(market, ticker);
CREATE INDEX IF NOT EXISTS idx_verdicts_ticker ON verdicts(market, ticker);
CREATE INDEX IF NOT EXISTS idx_verdicts_market_ticker_decided ON verdicts(market, ticker, decided_at);
CREATE INDEX IF NOT EXISTS idx_price_snapshots_security ON price_snapshots(market, ticker, fetched_at);

-- Cached chart history (read-through cache for /api/chart). Once a series is
-- fetched from the live provider it is persisted here so the chart stays
-- available even when the provider is rate-limited, unreachable, or returns no
-- intraday data for a symbol.
CREATE TABLE IF NOT EXISTS price_history (
    symbol TEXT NOT NULL,
    range_key TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (symbol, range_key)
);

-- Reusable Portfolio Groups (file/folder-like collections of securities).
CREATE TABLE IF NOT EXISTS portfolio_groups (
    group_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    strategy_id TEXT,
    strategy_name TEXT,
    created_from_strategy_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pg_source ON portfolio_groups(source);

CREATE TABLE IF NOT EXISTS portfolio_group_members (
    group_id TEXT NOT NULL,
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (group_id, market, ticker)
);
CREATE INDEX IF NOT EXISTS idx_pgm_group ON portfolio_group_members(group_id);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def execute(self, sql: str, params: tuple = ()) -> None:
        """Run a statement for health/startup checks (no result set needed)."""
        with self.connect() as conn:
            conn.execute(sql, params)

    def init_schema(self) -> int:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            # Legacy discovery registry: useful data was migrated into `securities`
            # (source='discovered'); the old table is now obsolete.
            conn.execute("DROP TABLE IF EXISTS discovered_tickers")
            self._migrate_verdicts(conn)
            self._migrate_user_ownership(conn)
            self._migrate_user_username(conn)
            self._migrate_paper_v2(conn)
            self._migrate_price_snapshot_status(conn)
            changed = self._migrate_verdict_reasons(conn)
        # Recorded migrations (idempotent; stamps baseline, applies the rest).
        try:
            from .migrations import upgrade

            upgrade(self)
        except Exception as exc:  # pragma: no cover - never block startup on stamps
            logger.warning("schema_migrations upgrade skipped: %s", exc)
        return changed

    @staticmethod
    def _migrate_price_snapshot_status(conn: sqlite3.Connection) -> None:
        """Add last-known-good status columns so a failed refresh can be marked
        STALE instead of reverting the whole security to NO_DATA.

        Idempotent; existing rows default to 'ready' (they were inserted when a
        successful fetch occurred).
        """
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(price_snapshots)")}
        if "data_status" not in cols:
            conn.execute(
                "ALTER TABLE price_snapshots ADD COLUMN data_status TEXT NOT NULL DEFAULT 'ready'"
            )
        if "as_of" not in cols:
            conn.execute(
                "ALTER TABLE price_snapshots ADD COLUMN as_of TEXT NOT NULL DEFAULT ''"
            )

    @contextmanager
    def transaction(self):
        """Yield a single connection wrapped in a BEGIN/COMMIT/ROLLBACK txn.

        Use this when several writes must be atomic (e.g. the paper fill engine).
        Inside the block, run SQL directly on the yielded connection — the per-
        operation helpers (pt_*) open their own connections and must NOT be used.
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _migrate_user_ownership(conn: sqlite3.Connection) -> None:
        """Safe migration: add ``user_id`` to paper tables for per-user ownership.

        Idempotent. Legacy rows keep the empty ``user_id`` (anonymous) — they are
        never deleted or silently reassigned to another user.
        """
        for table in ("paper_portfolio", "paper_orders"):
            cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if "user_id" not in cols:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN user_id TEXT NOT NULL DEFAULT ''"
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_user ON paper_orders(user_id, executed_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_portfolio_user ON paper_portfolio(user_id)"
        )

    @staticmethod
    def _migrate_user_username(conn: sqlite3.Connection) -> None:
        """Safe, idempotent migration: add ``username`` to the users table.

        Existing users get an empty username (the greeting falls back to the
        email prefix) until they register anew with one.
        """
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "username" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN username TEXT NOT NULL DEFAULT ''"
            )

    @staticmethod
    def _migrate_paper_v2(conn: sqlite3.Connection) -> None:
        """Forward-compat hook for the Fincept-style pt_* paper engine.

        The pt_* tables are created by SCHEMA (CREATE TABLE IF NOT EXISTS) which
        init_schema has already executed. Add future additive column migrations
        here; legacy paper_portfolio / paper_orders rows are never touched.
        """
        # No additive columns yet — tables are created by SCHEMA.
        return None

    @staticmethod
    def _migrate_verdicts(conn: sqlite3.Connection) -> None:
        """Backward-compatible migration: add new signal columns if missing."""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(verdicts)")}
        additions: dict[str, str] = {
            "lstm_score": "REAL NOT NULL DEFAULT 0",
            "lstm_probability_up": "REAL",
            "lstm_predicted_return": "REAL",
            "lstm_confidence": "REAL",
            "technical_score": "REAL NOT NULL DEFAULT 0",
            "signals": "TEXT NOT NULL DEFAULT ''",
        }
        for name, ddl in additions.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE verdicts ADD COLUMN {name} {ddl}")

    @staticmethod
    def _migrate_verdict_reasons(conn: sqlite3.Connection) -> int:
        """Rewrite legacy verdict reasons to the current canonical news format.

        Idempotent: only rows still carrying the legacy ``Auxiliary News
        Sentiment:`` marker (and not the current ``News:`` marker) are rewritten,
        so running it more than once is safe. Historical information (news label +
        score) is preserved; article count is taken from scored news items when
        available.
        """
        import re

        rows = conn.execute(
            "SELECT id, market, ticker, reason, news_score FROM verdicts"
        ).fetchall()
        legacy = [
            r
            for r in rows
            if "Auxiliary News Sentiment:" in (r["reason"] or "")
            and "News:" not in (r["reason"] or "")
        ]
        if not legacy:
            return 0
        counts: dict[tuple[str, str], int] = {}
        for r in conn.execute(
            """SELECT n.market, n.ticker, COUNT(*) AS c
               FROM news_items n
               WHERE EXISTS (SELECT 1 FROM sentiment_scores s WHERE s.news_item_id = n.id)
               GROUP BY n.market, n.ticker"""
        ):
            counts[(r["market"], r["ticker"])] = int(r["c"])
        changed = 0
        for row in legacy:
            reason = row["reason"] or ""
            label_m = re.search(r"Auxiliary News Sentiment:\s*(\w+)", reason)
            label = label_m.group(1).lower() if label_m else "neutral"
            if label == "none":
                new_clause = "News: unavailable"
            else:
                score = float(row["news_score"] or 0.0)
                count = counts.get((row["market"], row["ticker"]), 0)
                new_clause = f"News: {label} ({count} articles, score {score:+.2f})"
            new_reason = re.sub(
                r"Auxiliary News Sentiment:\s*\w+\s*\([^)]*\)", new_clause, reason
            )
            conn.execute(
                "UPDATE verdicts SET reason=? WHERE id=?", (new_reason, row["id"])
            )
            changed += 1
        logger.info("Migrated %d legacy verdict reasons to canonical format", changed)
        return changed

    # ---- Authentication ----

    def create_user(
        self, user_id: str, email: str, password_hash: str, username: str = ""
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO users (id, email, username, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, email.lower().strip(), username.strip(), password_hash, utc_now(), utc_now()),
            )

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(row) if row else None

    def create_session(self, token_hash: str, user_id: str, expires_at: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token_hash, user_id, utc_now(), expires_at),
            )

    def get_session(self, token_hash: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)
            ).fetchone()
            return dict(row) if row else None

    def delete_session(self, token_hash: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def insert_news_item(
        self,
        market: str,
        ticker: str,
        title: str,
        url: str,
        source: str = "",
        summary: str = "",
        published_at: str = "",
    ) -> int | None:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO news_items
                   (market, ticker, source, title, url, summary, published_at, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    market,
                    ticker.upper(),
                    source,
                    title,
                    url,
                    summary,
                    published_at,
                    utc_now(),
                ),
            )
            if cur.rowcount == 0:
                return None
            return cur.lastrowid

    def insert_sentiment(
        self,
        news_item_id: int,
        model: str,
        score: float,
        label: str,
        positive: float = 0.0,
        negative: float = 0.0,
        neutral: float = 0.0,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO sentiment_scores
                   (news_item_id, model, score, label, positive, negative, neutral, scored_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    news_item_id,
                    model,
                    score,
                    label,
                    positive,
                    negative,
                    neutral,
                    utc_now(),
                ),
            )

    def find_news_item_id(self, market: str, ticker: str, url: str) -> int | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM news_items WHERE market = ? AND ticker = ? AND url = ?",
                (market, ticker.upper(), url),
            ).fetchone()
            return int(row["id"]) if row else None

    def insert_price_snapshot(
        self,
        market: str,
        ticker: str,
        close: float,
        open: float = 0.0,
        high: float = 0.0,
        low: float = 0.0,
        volume: int = 0,
        momentum_20: float = 0.0,
        rsi_14: float = 50.0,
        sma_50: float = 0.0,
        data_status: str = "ready",
        as_of: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO price_snapshots
                   (market, ticker, fetched_at, close, open, high, low, volume, momentum_20, rsi_14, sma_50, data_status, as_of)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    market,
                    ticker.upper(),
                    utc_now(),
                    close,
                    open,
                    high,
                    low,
                    volume,
                    momentum_20,
                    rsi_14,
                    sma_50,
                    data_status,
                    as_of,
                ),
            )

    def insert_verdict(
        self,
        market: str,
        ticker: str,
        verdict: str,
        confidence: float,
        news_score: float,
        price_score: float,
        combined_score: float,
        reason: str,
        lstm_score: float = 0.0,
        lstm_probability_up: float | None = None,
        lstm_predicted_return: float | None = None,
        lstm_confidence: float | None = None,
        technical_score: float | None = None,
        signals: str = "",
    ) -> None:
        technical = price_score if technical_score is None else technical_score
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO verdicts
                   (market, ticker, verdict, confidence, news_score, price_score,
                    combined_score, reason, decided_at, lstm_score, lstm_probability_up,
                    lstm_predicted_return, lstm_confidence, technical_score, signals)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    market,
                    ticker.upper(),
                    verdict,
                    confidence,
                    news_score,
                    price_score,
                    combined_score,
                    reason,
                    utc_now(),
                    lstm_score,
                    lstm_probability_up,
                    lstm_predicted_return,
                    lstm_confidence,
                    technical,
                    signals,
                ),
            )

    def recent_news(
        self, market: str, ticker: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT n.*, s.score AS sentiment_score, s.label AS sentiment_label
                   FROM news_items n
                   LEFT JOIN sentiment_scores s ON s.news_item_id = n.id
                   WHERE n.market = ? AND n.ticker = ?
                   ORDER BY n.published_at DESC
                    LIMIT ?""",
                (market, ticker.upper(), limit),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["title"] = _clean_headline(d.get("title"), d.get("source"))
                d["summary"] = _clean_summary(
                    d.get("summary"), d["title"], d.get("source")
                )
                out.append(d)
            return out

    def recent_news_feed(self, limit: int = 40) -> list[dict[str, Any]]:
        """Most recent news across the whole universe with their latest sentiment.

        Used by the live event feed; the join picks the newest sentiment score per
        article so a multi-model article never appears twice. Sorts by fetched_at
        (always ISO, reliable) then published_at as a secondary signal so the
        newest articles truly come first even when published_at is empty or in
        a legacy non-ISO format.
        """
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT n.market, n.ticker, n.source, n.title, n.url, n.summary,
                          n.published_at, n.fetched_at,
                          s.score AS sentiment_score, s.label AS sentiment_label
                   FROM news_items n
                   LEFT JOIN sentiment_scores s
                     ON s.news_item_id = n.id
                    AND s.scored_at = (
                        SELECT MAX(s2.scored_at) FROM sentiment_scores s2
                        WHERE s2.news_item_id = n.id
                    )
                   ORDER BY n.fetched_at DESC,
                            CASE WHEN n.published_at = '' THEN n.fetched_at ELSE n.published_at END DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["security_id"] = f"{d['market']}:{d['ticker']}"
            d["title"] = _clean_headline(d.get("title"), d.get("source"))
            d["summary"] = _clean_summary(
                d.get("summary"), d["title"], d.get("source")
            )
            out.append(d)
        return out

    def latest_verdicts(self, market: str | None = None) -> list[dict[str, Any]]:
        # Correlated MAX subquery is fastest here: the UNIQUE(market, ticker,
        # decided_at) index makes the per-group MAX a cheap index seek, so only
        # the latest row per security (not the whole table) is returned.
        with self.connect() as conn:
            if market:
                rows = conn.execute(
                    """SELECT * FROM verdicts v
                       WHERE v.market = ?
                         AND v.decided_at = (
                             SELECT MAX(v2.decided_at) FROM verdicts v2
                             WHERE v2.market = v.market AND v2.ticker = v.ticker
                         )
                       ORDER BY v.combined_score DESC""",
                    (market,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM verdicts v
                       WHERE v.decided_at = (
                             SELECT MAX(v2.decided_at) FROM verdicts v2
                             WHERE v2.market = v.market AND v2.ticker = v.ticker
                         )
                       ORDER BY v.combined_score DESC"""
                ).fetchall()
            return [dict(r) for r in rows]

    def add_to_watchlist(self, market: str, ticker: str, company: str = "") -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO watchlist (market, ticker, company, added_at)
                   VALUES (?, ?, ?, ?)""",
                (market, ticker.upper(), company, utc_now()),
            )
            return cur.rowcount > 0

    def remove_from_watchlist(self, market: str, ticker: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM watchlist WHERE market = ? AND ticker = ?",
                (market, ticker.upper()),
            )
            return cur.rowcount > 0

    def watchlist(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM watchlist ORDER BY added_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def insert_recommendations(self, recommendations: list[dict[str, Any]]) -> None:
        if not recommendations:
            return
        generated = utc_now()
        with self.connect() as conn:
            conn.executemany(
                """INSERT INTO agent_recommendations
                   (market, ticker, company, action, confidence, rationale, generated_at)
                   VALUES (:market, :ticker, :company, :action, :confidence, :rationale, :generated_at)""",
                [
                    {
                        "market": r.get("market", ""),
                        "ticker": r.get("ticker", "").upper(),
                        "company": r.get("company", ""),
                        "action": r.get("action", ""),
                        "confidence": float(r.get("confidence", 0.0)),
                        "rationale": r.get("rationale", ""),
                        "generated_at": generated,
                    }
                    for r in recommendations
                ],
            )

    def latest_recommendations(self, market: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if market:
                rows = conn.execute(
                    """SELECT * FROM agent_recommendations
                       WHERE market = ?
                         AND generated_at = (
                             SELECT MAX(generated_at) FROM agent_recommendations
                             WHERE market = ?
                         )
                       ORDER BY confidence DESC""",
                    (market, market),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM agent_recommendations
                       WHERE generated_at = (
                           SELECT MAX(generated_at) FROM agent_recommendations
                       )
                       ORDER BY confidence DESC"""
                ).fetchall()
            return [dict(r) for r in rows]

    def upsert_security(
        self,
        market: str,
        ticker: str,
        symbol: str = "",
        company: str = "",
        exchange: str = "",
        currency: str = "",
        source: str = "configured",
        data_status: str = "no_data",
        last_analysis_at: str = "",
    ) -> None:
        """Register a security in the canonical universe.

        ``source`` is sticky: a configured security keeps its 'configured'
        source even if later re-registered by discovery or analysis.
        """
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO securities
                    (market, ticker, symbol, company, exchange, currency, source,
                     data_status, last_analysis_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market, ticker) DO UPDATE SET
                      symbol=excluded.symbol,
                      company=excluded.company,
                      exchange=excluded.exchange,
                      currency=excluded.currency,
                      source=CASE WHEN securities.source = 'configured' THEN 'configured' ELSE excluded.source END,
                      data_status=excluded.data_status,
                      last_analysis_at=COALESCE(excluded.last_analysis_at, securities.last_analysis_at),
                      updated_at=excluded.updated_at""",
                (
                    market,
                    ticker.upper(),
                    symbol,
                    company,
                    exchange,
                    currency,
                    source,
                    data_status,
                    last_analysis_at,
                    utc_now(),
                ),
            )

    def upsert_securities_bulk(self, rows: list[dict[str, Any]]) -> None:
        """Seed many securities in a single transaction.

        Used by :func:`universe.ensure_seeded` so the universe can be registered
        without opening a new DB connection per ticker (which made ``universe()``
        take ~1s on every screener call).
        """
        if not rows:
            return
        with self.connect() as conn:
            conn.executemany(
                """INSERT INTO securities
                    (market, ticker, symbol, company, exchange, currency, source,
                     data_status, last_analysis_at, updated_at)
                    VALUES (:market, :ticker, :symbol, :company, :exchange, :currency,
                            :source, :data_status, :last_analysis_at, :updated_at)
                    ON CONFLICT(market, ticker) DO UPDATE SET
                      symbol=excluded.symbol,
                      company=excluded.company,
                      exchange=excluded.exchange,
                      currency=excluded.currency,
                      source=CASE WHEN securities.source = 'configured' THEN 'configured' ELSE excluded.source END,
                      data_status=excluded.data_status,
                      last_analysis_at=COALESCE(excluded.last_analysis_at, securities.last_analysis_at),
                      updated_at=excluded.updated_at""",
                [
                    {
                        "market": r["market"],
                        "ticker": r["ticker"].upper(),
                        "symbol": r.get("symbol", ""),
                        "company": r.get("company", ""),
                        "exchange": r.get("exchange", ""),
                        "currency": r.get("currency", ""),
                        "source": r.get("source", "configured"),
                        "data_status": r.get("data_status", "no_data"),
                        "last_analysis_at": r.get("last_analysis_at", ""),
                        "updated_at": utc_now(),
                    }
                    for r in rows
                ],
            )

    def all_securities(self, market: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if market:
                rows = conn.execute(
                    "SELECT * FROM securities WHERE market = ? ORDER BY market, ticker",
                    (market,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM securities ORDER BY market, ticker"
                ).fetchall()
            return [dict(r) for r in rows]

    def securities_map(self) -> dict[tuple[str, str], dict[str, Any]]:
        return {(s["market"], s["ticker"].upper()): s for s in self.all_securities()}

    # ---- Decision snapshots (paper research engine) ----

    def insert_decision_snapshot(
        self,
        decision_id: str,
        market: str,
        ticker: str,
        decided_at: str,
        verdict: str,
        conviction: float | None,
        reference_price: float | None,
        research_confidence: float | None,
        decision_json: str = "",
    ) -> bool:
        """Append-only snapshot. Returns True if inserted, False if a snapshot
        for the same (market, ticker, decided_at) already exists."""
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO decision_snapshots
                   (decision_id, security_id, market, ticker, decided_at, verdict,
                    conviction, reference_price, research_confidence, decision_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision_id,
                    f"{market}:{ticker.upper()}",
                    market,
                    ticker.upper(),
                    decided_at,
                    verdict,
                    conviction,
                    reference_price,
                    research_confidence,
                    decision_json,
                ),
            )
            return cur.rowcount > 0

    def decision_snapshots(
        self, market: str | None = None, ticker: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            sql = "SELECT * FROM decision_snapshots"
            conds: list[str] = []
            params: list[Any] = []
            if market:
                conds.append("market = ?")
                params.append(market)
            if ticker:
                conds.append("ticker = ?")
                params.append(ticker.upper())
            if conds:
                sql += " WHERE " + " AND ".join(conds)
            sql += " ORDER BY decided_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    # ---- Paper trading (simulation only) ----

    def upsert_paper_portfolio(
        self,
        session_id: str,
        starting_cash: float,
        currency: str = "USD",
        user_id: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO paper_portfolio (session_id, starting_cash, currency, opened_at, active, user_id)
                   VALUES (?, ?, ?, ?, 1, ?)""",
                (session_id, starting_cash, currency, utc_now(), user_id),
            )

    def active_portfolio(self, user_id: str = "") -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_portfolio WHERE active = 1 AND user_id = ? ORDER BY opened_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def insert_paper_order(
        self,
        order_id: str,
        session_id: str,
        market: str,
        ticker: str,
        side: str,
        quantity: float,
        price: float,
        fee: float,
        executed_at: str,
        decision_id: str | None,
        reason: str,
        user_id: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO paper_orders
                   (order_id, session_id, security_id, market, ticker, side, quantity,
                    price, fee, executed_at, decision_id, reason, user_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order_id,
                    session_id,
                    f"{market}:{ticker.upper()}",
                    market,
                    ticker.upper(),
                    side,
                    quantity,
                    price,
                    fee,
                    executed_at,
                    decision_id,
                    reason,
                    user_id,
                ),
            )

    def paper_orders(self, session_id: str | None = None, user_id: str = "") -> list[dict[str, Any]]:
        with self.connect() as conn:
            if session_id:
                rows = conn.execute(
                    "SELECT * FROM paper_orders WHERE session_id = ? AND user_id = ? ORDER BY executed_at",
                    (session_id, user_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM paper_orders WHERE user_id = ? ORDER BY executed_at",
                    (user_id,),
                ).fetchall()
            return [dict(r) for r in rows]

    # ---- Paper trading v2 (Fincept-style pt_* engine) ----

    def pt_insert_portfolio(self, p: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO pt_portfolios
                   (id, name, user_id, initial_balance, balance, currency, leverage,
                    margin_mode, fee_rate, exchange, enforce_market_hours, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    p["id"], p["name"], p.get("user_id", ""), p["initial_balance"],
                    p["balance"], p.get("currency", "USD"), p.get("leverage", 1.0),
                    p.get("margin_mode", "cross"), p.get("fee_rate", 0.001),
                    p.get("exchange", ""), 1 if p.get("enforce_market_hours") else 0,
                    p["created_at"],
                ),
            )

    def pt_get_portfolio(self, portfolio_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pt_portfolios WHERE id = ?", (portfolio_id,)
            ).fetchone()
            return dict(row) if row else None

    def pt_list_portfolios(self, user_id: str = "") -> list[dict[str, Any]]:
        with self.connect() as conn:
            if user_id:
                rows = conn.execute(
                    "SELECT * FROM pt_portfolios WHERE user_id = ? ORDER BY created_at DESC",
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM pt_portfolios ORDER BY created_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    def pt_find_portfolio(self, name: str, user_id: str = "") -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pt_portfolios WHERE name = ? AND user_id = ? LIMIT 1",
                (name, user_id),
            ).fetchone()
            return dict(row) if row else None

    def pt_update_balance(self, portfolio_id: str, balance: float) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE pt_portfolios SET balance = ? WHERE id = ?",
                (balance, portfolio_id),
            )

    def pt_set_enforce_market_hours(self, portfolio_id: str, enforce: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE pt_portfolios SET enforce_market_hours = ? WHERE id = ?",
                (1 if enforce else 0, portfolio_id),
            )

    def pt_delete_portfolio(self, portfolio_id: str) -> None:
        with self.connect() as conn:
            for t in ("pt_trades", "pt_margin_blocks", "pt_positions", "pt_orders"):
                conn.execute(f"DELETE FROM {t} WHERE portfolio_id = ?", (portfolio_id,))
            conn.execute("DELETE FROM pt_portfolios WHERE id = ?", (portfolio_id,))

    def pt_insert_order(self, o: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO pt_orders
                   (id, portfolio_id, user_id, security_id, market, ticker, side,
                    order_type, quantity, price, stop_price, filled_qty, avg_price,
                    status, reduce_only, margin_blocked, product, exchange,
                    decision_id, reason, created_at, filled_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    o["id"], o["portfolio_id"], o.get("user_id", ""), o.get("security_id", ""),
                    o.get("market", ""), o.get("ticker", ""), o["side"], o["order_type"],
                    o["quantity"], o.get("price"), o.get("stop_price"), o.get("filled_qty", 0.0),
                    o.get("avg_price"), o.get("status", "pending"), 1 if o.get("reduce_only") else 0,
                    o.get("margin_blocked", 0.0), o.get("product", ""), o.get("exchange", ""),
                    o.get("decision_id"), o.get("reason", ""), o["created_at"], o.get("filled_at"),
                ),
            )

    def pt_get_order(self, order_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pt_orders WHERE id = ?", (order_id,)).fetchone()
            return dict(row) if row else None

    def pt_update_order_fill(
        self, order_id: str, filled_qty: float, avg_price: float, status: str, filled_at: str
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE pt_orders SET filled_qty = ?, avg_price = ?, status = ?, filled_at = ? WHERE id = ?",
                (filled_qty, avg_price, status, filled_at, order_id),
            )

    def pt_cancel_order(self, order_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE pt_orders SET status = 'cancelled' WHERE id = ?", (order_id,)
            )

    def pt_get_orders(self, portfolio_id: str, status: str = "") -> list[dict[str, Any]]:
        with self.connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM pt_orders WHERE portfolio_id = ? AND status = ? ORDER BY created_at",
                    (portfolio_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM pt_orders WHERE portfolio_id = ? ORDER BY created_at DESC",
                    (portfolio_id,),
                ).fetchall()
            return [dict(r) for r in rows]

    def pt_get_orders_between(self, portfolio_id: str, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pt_orders WHERE portfolio_id = ? AND created_at >= ? AND created_at < ? ORDER BY created_at",
                (portfolio_id, start_iso, end_iso),
            ).fetchall()
            return [dict(r) for r in rows]

    def pt_insert_position(self, p: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO pt_positions
                   (id, portfolio_id, user_id, security_id, market, ticker, side,
                    quantity, entry_price, current_price, unrealized_pnl, realized_pnl,
                    leverage, product, held_margin, opened_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    p["id"], p["portfolio_id"], p.get("user_id", ""), p.get("security_id", ""),
                    p.get("market", ""), p.get("ticker", ""), p["side"], p["quantity"],
                    p["entry_price"], p.get("current_price", 0.0), p.get("unrealized_pnl", 0.0),
                    p.get("realized_pnl", 0.0), p.get("leverage", 1.0), p.get("product", "MIS"),
                    p.get("held_margin", 0.0), p["opened_at"],
                ),
            )

    def pt_get_position(self, position_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pt_positions WHERE id = ?", (position_id,)).fetchone()
            return dict(row) if row else None

    def pt_find_position(self, portfolio_id: str, market: str, ticker: str, side: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pt_positions WHERE portfolio_id = ? AND market = ? AND ticker = ? AND side = ? LIMIT 1",
                (portfolio_id, market, ticker.upper(), side),
            ).fetchone()
            return dict(row) if row else None

    def pt_get_positions(self, portfolio_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pt_positions WHERE portfolio_id = ? ORDER BY opened_at",
                (portfolio_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def pt_update_position(self, position_id: str, quantity: float, entry_price: float) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE pt_positions SET quantity = ?, entry_price = ? WHERE id = ?",
                (quantity, entry_price, position_id),
            )

    def pt_set_position_margin(self, position_id: str, held_margin: float) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE pt_positions SET held_margin = ? WHERE id = ?",
                (held_margin, position_id),
            )

    def pt_add_realized_pnl(self, position_id: str, pnl: float) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE pt_positions SET realized_pnl = realized_pnl + ? WHERE id = ?",
                (pnl, position_id),
            )

    def pt_set_position_product(self, position_id: str, product: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE pt_positions SET product = ? WHERE id = ?", (product, position_id)
            )

    def pt_update_position_price(self, portfolio_id: str, market: str, ticker: str, price: float) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE pt_positions SET current_price = ? WHERE portfolio_id = ? AND market = ? AND ticker = ?",
                (price, portfolio_id, market, ticker.upper()),
            )

    def pt_delete_position(self, position_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM pt_positions WHERE id = ?", (position_id,))

    def pt_insert_trade(self, t: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO pt_trades
                   (id, portfolio_id, order_id, security_id, market, ticker, side,
                    price, quantity, fee, pnl, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    t["id"], t["portfolio_id"], t["order_id"], t.get("security_id", ""),
                    t.get("market", ""), t.get("ticker", ""), t["side"], t["price"],
                    t["quantity"], t.get("fee", 0.0), t.get("pnl", 0.0), t["timestamp"],
                ),
            )

    def pt_get_trades(self, portfolio_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pt_trades WHERE portfolio_id = ? ORDER BY timestamp DESC LIMIT ?",
                (portfolio_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def pt_get_trades_between(self, portfolio_id: str, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pt_trades WHERE portfolio_id = ? AND timestamp >= ? AND timestamp < ? ORDER BY timestamp",
                (portfolio_id, start_iso, end_iso),
            ).fetchall()
            return [dict(r) for r in rows]

    def pt_insert_margin_block(self, block_id: str, portfolio_id: str, order_id: str, security_id: str, amount: float) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO pt_margin_blocks (id, portfolio_id, order_id, security_id, amount) VALUES (?, ?, ?, ?, ?)",
                (block_id, portfolio_id, order_id, security_id, amount),
            )

    def pt_get_margin_block(self, order_id: str) -> float:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT amount FROM pt_margin_blocks WHERE order_id = ? LIMIT 1", (order_id,)
            ).fetchone()
            return float(row["amount"]) if row else 0.0

    def pt_delete_margin_block(self, order_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM pt_margin_blocks WHERE order_id = ?", (order_id,))

    # ---- Historical decision evaluation ----

    def insert_decision_evaluation(
        self,
        decision_id: str,
        reference_price: float | None,
        prices: dict[str, float | None],
        correct: int | None,
        status: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO decision_evaluations
                   (decision_id, reference_price, p5, p15, p30, p60, close_price, correct, status, evaluated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision_id,
                    reference_price,
                    prices.get("p5"),
                    prices.get("p15"),
                    prices.get("p30"),
                    prices.get("p60"),
                    prices.get("close"),
                    correct,
                    status,
                    utc_now(),
                ),
            )

    def decision_evaluations(self) -> dict[str, dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM decision_evaluations").fetchall()
            return {r["decision_id"]: dict(r) for r in rows}

    # ---- Paper equity curve ----

    def insert_equity_point(self, session_id: str, equity: float) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO paper_equity_points (session_id, recorded_at, equity) VALUES (?, ?, ?)",
                (session_id, utc_now(), equity),
            )

    def equity_points(self, session_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_equity_points WHERE session_id = ? ORDER BY recorded_at",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def last_equity_at(self, session_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT recorded_at FROM paper_equity_points WHERE session_id = ? ORDER BY recorded_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            return row["recorded_at"] if row else None

    # ---- Backtest runs / snapshots ----

    def insert_backtest_run(
        self, run_id: str, mode: str, market: str, ticker: str, period: str, capital: float
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO backtest_runs (run_id, mode, market, ticker, period, capital, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, mode, market, ticker.upper(), period, capital, utc_now()),
            )

    def insert_backtest_snapshot(
        self,
        run_id: str,
        decision_id: str,
        market: str,
        ticker: str,
        ts: str,
        verdict: str,
        conviction: float | None,
        reference_price: float | None,
        signals_json: str,
        forward_json: str,
        correct: int | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO backtest_snapshots
                   (run_id, decision_id, security_id, market, ticker, ts, verdict,
                    conviction, reference_price, signals_json, forward_json, correct)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, decision_id, f"{market}:{ticker.upper()}", market, ticker.upper(), ts, verdict,
                 conviction, reference_price, signals_json, forward_json, correct),
            )

    # ---- Historical replay runs / immutable decision snapshots ----

    def insert_replay_run(
        self,
        run_id: str,
        market: str,
        ticker: str,
        start_date: str,
        end_date: str,
        timeframe: str,
        interval_minutes: int,
        capital: float,
        summary_json: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO replay_runs
                   (run_id, market, ticker, start_date, end_date, timeframe,
                    interval_minutes, capital, summary_json, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, market, ticker.upper(), start_date, end_date, timeframe,
                 interval_minutes, capital, summary_json, utc_now()),
            )

    def insert_replay_decision(
        self,
        run_id: str,
        decision_id: str,
        market: str,
        ticker: str,
        ts: str,
        action: str,
        verdict: str,
        conviction: float | None,
        reference_price: float | None,
        execution_price: float | None,
        quantity: float,
        cash: float,
        equity: float,
        position_direction: str | None,
        position_qty: float,
        reason: str,
        detail_json: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO replay_decisions
                   (run_id, decision_id, security_id, market, ticker, ts, action, verdict,
                    conviction, reference_price, execution_price, quantity, cash, equity,
                    position_direction, position_qty, reason, detail_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, decision_id, f"{market}:{ticker.upper()}", market, ticker.upper(), ts, action, verdict,
                 conviction, reference_price, execution_price, quantity, cash, equity,
                 position_direction, position_qty, reason, detail_json),
            )

    def latest_replay_run(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM replay_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def replay_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM replay_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def replay_decisions(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM replay_decisions WHERE run_id = ? ORDER BY ts",
                (run_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- Notification events (deterministic keys, append-only) ----

    def insert_notification_event(
        self,
        event_key: str,
        severity: str,
        type_: str,
        title: str,
        message: str,
        security_id: str = "",
        market: str = "",
        ticker: str = "",
        payload: str = "",
    ) -> bool:
        """Insert an event only if its key is new. Returns True when inserted."""
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO notification_events
                   (event_key, severity, type, title, message, security_id,
                    market, ticker, payload_json, created_at, acked)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (event_key, severity, type_, title, message, security_id,
                 market, ticker.upper(), payload, utc_now()),
            )
            return cur.rowcount > 0

    def mark_notification_processed(self, event_key: str) -> bool:
        """Record an event key as processed (dedup across polls)."""
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO notification_processed (event_key, processed_at) VALUES (?, ?)",
                (event_key, utc_now()),
            )
            return cur.rowcount > 0

    def is_notification_processed(self, event_key: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM notification_processed WHERE event_key = ?", (event_key,)
            ).fetchone()
            return row is not None

    def notifications(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM notification_events
                   ORDER BY acked ASC, created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def ack_notifications(self, keys: list[str]) -> int:
        if not keys:
            return 0
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE notification_events SET acked = 1 WHERE event_key IN ({','.join('?' * len(keys))})",
                keys,
            )
            return cur.rowcount

    # ---- User-defined price alert rules ----

    def create_price_alert(
        self,
        market: str,
        ticker: str,
        direction: str,
        target_price: float,
        note: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO price_alert_rules
                   (market, ticker, direction, target_price, note, active,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    market.upper(), ticker.upper(), direction.lower(),
                    float(target_price), note.strip()[:240], now, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM price_alert_rules WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return dict(row)

    def price_alerts(self, active_only: bool = False) -> list[dict[str, Any]]:
        with self.connect() as conn:
            sql = "SELECT * FROM price_alert_rules"
            params: tuple[Any, ...] = ()
            if active_only:
                sql += " WHERE active = ?"
                params = (1,)
            sql += " ORDER BY active DESC, created_at DESC"
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def update_price_alert(
        self,
        alert_id: int,
        *,
        active: bool | None = None,
        last_price: float | None = None,
        triggered_at: str | None = None,
    ) -> dict[str, Any] | None:
        fields: list[str] = []
        values: list[Any] = []
        if active is not None:
            fields.append("updated_at = ?")
            values.append(utc_now())
            fields.append("active = ?")
            values.append(1 if active else 0)
            if active:
                fields.append("triggered_at = NULL")
        if last_price is not None:
            fields.append("last_price = ?")
            values.append(float(last_price))
        if triggered_at is not None:
            if active is None:
                fields.append("updated_at = ?")
                values.append(utc_now())
            fields.append("triggered_at = ?")
            values.append(triggered_at)
        if not fields:
            return None
        values.append(int(alert_id))
        with self.connect() as conn:
            conn.execute(
                f"UPDATE price_alert_rules SET {', '.join(fields)} WHERE id = ?",
                values,
            )
            row = conn.execute(
                "SELECT * FROM price_alert_rules WHERE id = ?", (int(alert_id),)
            ).fetchone()
            return dict(row) if row else None

    def delete_price_alert(self, alert_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM price_alert_rules WHERE id = ?", (int(alert_id),)
            )
            return cur.rowcount > 0

    # ---- Verdict / price history pairs (for change + screener detection) ----

    def verdict_pairs(self, market: str | None = None) -> dict[tuple[str, str], tuple[dict[str, Any] | None, dict[str, Any] | None]]:
        """For every security: (latest_verdict, previous_verdict_or_None).

        Uses a window function so only the two most recent verdicts per security
        are read — not the entire verdicts table (which grows unbounded as the
        pipeline re-analyzes tickers over time).
        """
        return self._latest_prev_pairs("verdicts", "decided_at", market)

    def price_snapshot_pairs(self, market: str | None = None) -> dict[tuple[str, str], tuple[dict[str, Any] | None, dict[str, Any] | None]]:
        """For every security: (latest_price_snapshot, previous_or_None).

        Window-function backed: only the two most recent snapshots per security
        are materialized, so a large price history does not slow the screener.
        """
        return self._latest_prev_pairs("price_snapshots", "fetched_at", market)

    def _latest_prev_pairs(
        self, table: str, ts_col: str, market: str | None
    ) -> dict[tuple[str, str], tuple[dict[str, Any] | None, dict[str, Any] | None]]:
        """Return ``(market, ticker) -> (latest_row, previous_row_or_None)`` using a
        window function, reading only the two most recent rows per security."""
        with self.connect() as conn:
            if market:
                rows = conn.execute(
                    f"""SELECT * FROM (
                           SELECT {table}.*, ROW_NUMBER() OVER (
                               PARTITION BY market, ticker ORDER BY {ts_col} DESC
                           ) AS _rn
                           FROM {table} WHERE market = :m
                       ) WHERE _rn <= 2
                       ORDER BY market, ticker, {ts_col} DESC""",
                    {"m": market},
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""SELECT * FROM (
                           SELECT {table}.*, ROW_NUMBER() OVER (
                               PARTITION BY market, ticker ORDER BY {ts_col} DESC
                           ) AS _rn
                           FROM {table}
                       ) WHERE _rn <= 2
                       ORDER BY market, ticker, {ts_col} DESC"""
                ).fetchall()
        pairs: dict[tuple[str, str], tuple[dict[str, Any] | None, dict[str, Any] | None]] = {}
        for r in rows:
            d = {k: v for k, v in dict(r).items() if k != "_rn"}
            key = (d["market"], d["ticker"])
            latest, previous = pairs.get(key, (None, None))
            if latest is None:
                latest = d
            else:
                previous = d
            pairs[key] = (latest, previous)
        return pairs

    def upsert_fund_filing(
        self,
        cik: str,
        fund_name: str,
        form: str,
        accession: str,
        filing_date: str,
        period_of_report: str = "",
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO fund_filings (cik, fund_name, form, accession, filing_date, period_of_report, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(cik, accession) DO UPDATE SET
                     fund_name=excluded.fund_name, filing_date=excluded.filing_date,
                     period_of_report=excluded.period_of_report, fetched_at=excluded.fetched_at""",
                (
                    cik,
                    fund_name,
                    form,
                    accession,
                    filing_date,
                    period_of_report,
                    utc_now(),
                ),
            )
            row = conn.execute(
                "SELECT id FROM fund_filings WHERE cik = ? AND accession = ?",
                (cik, accession),
            ).fetchone()
            return int(row["id"])

    def replace_fund_holdings(
        self, fund_filing_id: int, holdings: list[dict[str, Any]]
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM fund_holdings WHERE fund_filing_id = ?", (fund_filing_id,)
            )
            conn.executemany(
                """INSERT INTO fund_holdings
                   (fund_filing_id, cusip, issuer, ticker, value_thousands, shares, shares_type, put_call, pct_portfolio)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        fund_filing_id,
                        h.get("cusip", ""),
                        h.get("issuer", ""),
                        h.get("ticker", ""),
                        h.get("value_thousands", h.get("value", 0.0)),
                        float(h.get("shares", 0.0)),
                        h.get("shares_type", "SH"),
                        h.get("put_call", ""),
                        float(h.get("pct_portfolio", 0.0)),
                    )
                    for h in holdings
                ],
            )

    def fund_filings(
        self, cik: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if cik:
                rows = conn.execute(
                    "SELECT * FROM fund_filings WHERE cik = ? ORDER BY filing_date DESC LIMIT ?",
                    (cik, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM fund_filings ORDER BY filing_date DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def fund_holdings(
        self, fund_filing_id: int, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM fund_holdings WHERE fund_filing_id = ? ORDER BY value_thousands DESC LIMIT ?",
                (fund_filing_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def latest_price_snapshot(self, market: str, ticker: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT * FROM price_snapshots
                   WHERE market = ? AND ticker = ?
                   ORDER BY fetched_at DESC LIMIT 1""",
                (market, ticker.upper()),
            ).fetchone()
            return dict(row) if row else None

    def latest_price_snapshots(self, market: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            # GROUP-BY join over the (market, ticker, fetched_at) index: one pass
            # to find each security's MAX, one index lookup per row. Much cheaper
            # than the previous per-row correlated MAX on large universes.
            if market:
                rows = conn.execute(
                    """SELECT p.* FROM price_snapshots p
                       JOIN (SELECT market, ticker, MAX(fetched_at) AS mf
                             FROM price_snapshots WHERE market = ?
                             GROUP BY market, ticker) g
                         ON p.market = g.market AND p.ticker = g.ticker AND p.fetched_at = g.mf""",
                    (market,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT p.* FROM price_snapshots p
                       JOIN (SELECT market, ticker, MAX(fetched_at) AS mf
                             FROM price_snapshots GROUP BY market, ticker) g
                         ON p.market = g.market AND p.ticker = g.ticker AND p.fetched_at = g.mf"""
                ).fetchall()
            return [dict(r) for r in rows]

    def latest_index_snapshots(self, market: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if market:
                rows = conn.execute(
                    """SELECT * FROM index_snapshots s
                       WHERE s.market = ?
                         AND s.fetched_at = (
                             SELECT MAX(s2.fetched_at) FROM index_snapshots s2
                             WHERE s2.market = s.market AND s2.symbol = s.symbol
                         )""",
                    (market,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM index_snapshots s
                       WHERE s.fetched_at = (
                           SELECT MAX(s2.fetched_at) FROM index_snapshots s2
                           WHERE s2.market = s.market AND s2.symbol = s.symbol
                       )"""
                ).fetchall()
            return [dict(r) for r in rows]

    # ---- Cached chart history (read-through cache for /api/chart) ----

    def _ensure_price_history(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS price_history (
                       symbol TEXT NOT NULL,
                       range_key TEXT NOT NULL,
                       fetched_at TEXT NOT NULL,
                       payload TEXT NOT NULL DEFAULT '[]',
                       PRIMARY KEY (symbol, range_key)
                   )"""
            )

    def upsert_price_history(
        self, symbol: str, range_key: str, rows: list[dict[str, Any]]
    ) -> None:
        self._ensure_price_history()
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO price_history (symbol, range_key, fetched_at, payload)
                   VALUES (?, ?, ?, ?)""",
                (symbol.upper(), range_key, utc_now(), json.dumps(rows, default=str)),
            )

    def get_price_history(
        self, symbol: str, range_key: str
    ) -> list[dict[str, Any]] | None:
        self._ensure_price_history()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM price_history WHERE symbol = ? AND range_key = ?",
                (symbol.upper(), range_key),
            ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["payload"])
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, list) else None

    def ticker_strip_snapshots(
        self, limit: int = 500, market: str | None = None
    ) -> list[dict[str, Any]]:
        """Securities that have real market data, for the overhead ticker strip.

        Returns the latest price snapshot per (market, ticker) joined with the
        universe `securities` table for display metadata, plus a `change_pct`
        computed as the **intraday** move ``(close - open) / open`` for the
        current session (``None`` when open is missing, so the UI can show
        NO_DATA rather than fabricate a move). When the market is open ``close``
        is the live price; when closed it is the final close — so the same
        formula satisfies both cases.
        """
        with self.connect() as conn:
            sql = """
                SELECT p.market, p.ticker, p.close, p.open, p.fetched_at,
                       s.company, s.exchange, s.currency
                FROM price_snapshots p
                LEFT JOIN securities s ON s.market = p.market AND s.ticker = p.ticker
                WHERE (p.market, p.ticker, p.fetched_at) IN (
                    SELECT market, ticker, MAX(fetched_at)
                    FROM price_snapshots GROUP BY market, ticker
                )
            """
            args: list[Any] = []
            if market:
                sql += " AND p.market = ?"
                args.append(market)
            sql += " ORDER BY p.market, p.ticker"
            if limit:
                sql += f" LIMIT {int(limit)}"
            rows = conn.execute(sql, args).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                o = d.get("open")
                c = d.get("close")
                try:
                    ov = float(o) if o not in (None, "") else 0.0
                    cv = float(c) if c not in (None, "") else 0.0
                except (TypeError, ValueError):
                    ov, cv = 0.0, 0.0
                if ov and cv:
                    d["change_pct"] = (cv - ov) / ov
                else:
                    d["change_pct"] = None
                out.append(d)
            return out

    # ---- Portfolio Groups -------------------------------------------------

    def create_group(
        self,
        group_id: str,
        name: str,
        description: str = "",
        source: str = "manual",
        strategy_id: str | None = None,
        strategy_name: str | None = None,
        created_from_strategy_at: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        if source in ("strategy", "agent_workflow") and not created_from_strategy_at:
            created_from_strategy_at = now
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO portfolio_groups
                   (group_id, name, description, created_at, updated_at, source,
                    strategy_id, strategy_name, created_from_strategy_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    group_id,
                    name,
                    description,
                    now,
                    now,
                    source,
                    strategy_id,
                    strategy_name,
                    created_from_strategy_at or "",
                ),
            )
        return self.get_group(group_id)

    def get_group(self, group_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM portfolio_groups WHERE group_id = ?", (group_id,)
            ).fetchone()
            if not row:
                return None
            g = dict(row)
            members = conn.execute(
                "SELECT market, ticker FROM portfolio_group_members WHERE group_id = ? ORDER BY added_at",
                (group_id,),
            ).fetchall()
            g["security_ids"] = [f"{m['market']}:{m['ticker']}" for m in members]
            g["members"] = [
                {"market": m["market"], "ticker": m["ticker"]} for m in members
            ]
            return g

    def list_groups(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM portfolio_groups ORDER BY updated_at DESC"
            ).fetchall()
            out = []
            for r in rows:
                g = dict(r)
                members = conn.execute(
                    "SELECT market, ticker FROM portfolio_group_members WHERE group_id = ? ORDER BY added_at",
                    (g["group_id"],),
                ).fetchall()
                g["security_ids"] = [f"{m['market']}:{m['ticker']}" for m in members]
                g["members"] = [
                    {"market": m["market"], "ticker": m["ticker"]} for m in members
                ]
                out.append(g)
            return out

    def rename_group(self, group_id: str, name: str, description: str | None = None) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE portfolio_groups SET name = ?, updated_at = ? WHERE group_id = ?",
                (name, utc_now(), group_id),
            )
            if description is not None:
                conn.execute(
                    "UPDATE portfolio_groups SET description = ? WHERE group_id = ?",
                    (description, group_id),
                )
        return self.get_group(group_id)

    def delete_group(self, group_id: str) -> bool:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM portfolio_group_members WHERE group_id = ?", (group_id,)
            )
            cur = conn.execute(
                "DELETE FROM portfolio_groups WHERE group_id = ?", (group_id,)
            )
            return cur.rowcount > 0

    def add_to_group(self, group_id: str, market: str, ticker: str) -> bool:
        market = market.upper()
        ticker = ticker.upper()
        # No duplicate securities inside the same group.
        existing = self.get_group(group_id)
        if existing and any(
            m["market"] == market and m["ticker"] == ticker
            for m in existing.get("members", [])
        ):
            return False
        with self.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO portfolio_group_members
                   (group_id, market, ticker, added_at) VALUES (?, ?, ?, ?)""",
                (group_id, market, ticker, utc_now()),
            )
            conn.execute(
                "UPDATE portfolio_groups SET updated_at = ? WHERE group_id = ?",
                (utc_now(), group_id),
            )
            return conn.total_changes > 0

    def remove_from_group(self, group_id: str, market: str, ticker: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM portfolio_group_members WHERE group_id = ? AND market = ? AND ticker = ?",
                (group_id, market.upper(), ticker.upper()),
            )
            if cur.rowcount > 0:
                conn.execute(
                    "UPDATE portfolio_groups SET updated_at = ? WHERE group_id = ?",
                    (utc_now(), group_id),
                )
            return cur.rowcount > 0
