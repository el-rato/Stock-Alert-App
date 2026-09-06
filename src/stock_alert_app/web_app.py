from __future__ import annotations

import logging
import re
import secrets
import time
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import settings
from .db import Database, utc_now
from .analysis import snapshot_price, technical_from_snapshot
from . import auth

from .logging_setup import setup_logging

setup_logging()

logger = logging.getLogger(__name__)

app = FastAPI(title="StockVerdict", version="0.1.0")

# ---- Production hardening: request ids, structured errors, startup checks ----


@app.middleware("http")
async def _request_context(request: Request, call_next):
    import uuid

    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "unhandled error %s %s", request.method, request.url.path,
            extra={"request_id": request_id, "path": request.url.path, "method": request.method},
        )
        return JSONResponse(
            {"detail": "internal server error", "request_id": request_id},
            status_code=500,
            headers={"X-Request-ID": request_id},
        )
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(HTTPException)
async def _http_exception(request: Request, exc: HTTPException):
    return JSONResponse(
        {"detail": exc.detail, "request_id": request.headers.get("X-Request-ID", "")},
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    request_id = request.headers.get("X-Request-ID", "")
    logger.exception(
        "unhandled error %s %s", request.method, request.url.path,
        extra={"request_id": request_id, "path": request.url.path, "method": request.method},
    )
    return JSONResponse(
        {"detail": "internal server error", "request_id": request_id},
        status_code=500,
    )


@app.on_event("startup")
def _startup_validation() -> None:
    """Fail fast on misconfiguration instead of serving broken traffic."""
    settings.validate_runtime()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    db.init_schema()
    db.execute("SELECT 1")
    logger.info(
        "startup ok: env=%s db=%s", settings.environment, settings.db_path
    )


@app.get("/api/health")
def health_live() -> dict[str, object]:
    """Liveness: the process is up and able to serve. No external dependencies."""
    return {"status": "ok", "environment": settings.environment}


@app.get("/api/health/ready")
def health_ready() -> dict[str, object]:
    """Readiness: database reachable AND recorded migrations up to date."""
    from . import migrations

    try:
        db = _db()
        db.init_schema()
        db.execute("SELECT 1")
        st = migrations.status(db)
    except Exception as exc:
        logger.warning("readiness failed: %s", exc)
        return JSONResponse({"status": "unready", "reason": str(exc)}, status_code=503)
    if not st["up_to_date"]:
        return JSONResponse(
            {"status": "unready", "reason": "pending migrations", "pending": st["pending"]},
            status_code=503,
        )
    return {"status": "ready", "environment": settings.environment, "migrations": st["applied"]}

UI_DIR = Path(__file__).resolve().parent / "web"
#: Frontend dist (configurable for artifact deployments via FRONTEND_DIST).
#: Empty/whitespace env value must fall back to the default — never to CWD.
_dist_env = (settings.frontend_dist or "").strip()
FRONTEND_DIR = (
    Path(_dist_env).resolve()
    if _dist_env
    else Path(__file__).resolve().parents[2] / "frontend" / "dist"
)


def _ui_assets_dir() -> Path:
    if (FRONTEND_DIR / "index.html").is_file():
        return FRONTEND_DIR
    return UI_DIR


class WatchItem(BaseModel):
    market: str
    ticker: str
    company: str = ""


class PaperOrder(BaseModel):
    portfolio_id: str = ""
    market: str
    ticker: str
    side: str                       # 'buy' | 'sell'
    order_type: str = "market"      # 'market' | 'limit' | 'stop' | 'stop_limit'
    quantity: float
    price: float | None = None
    stop_price: float | None = None
    reduce_only: bool = False
    product: str = ""               # 'MIS' | 'CNC' | ''
    exchange: str = ""
    decision_id: str = ""
    reason: str = ""


class PaperPortfolioCreate(BaseModel):
    name: str
    balance: float = 100000.0
    currency: str = "USD"
    leverage: float = 1.0
    margin_mode: str = "cross"
    fee_rate: float = 0.001
    exchange: str = ""


class ReplayRequest(BaseModel):
    market: str
    ticker: str
    start_date: str
    end_date: str
    timeframe: str = "15m"
    decision_interval: str = "15m"
    capital: float = 100000.0
    bull_threshold: float = 70.0
    bear_threshold: float = 70.0
    size_ratio: float = 0.25


class AckRequest(BaseModel):
    keys: list[str]


class PriceAlertCreate(BaseModel):
    market: str
    ticker: str
    direction: str
    target_price: float
    note: str = ""


class PriceAlertUpdate(BaseModel):
    active: bool


class RegisterRequest(BaseModel):
    email: str
    password: str
    username: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str


_initialized_dbs: set[str] = set()

#: Throttle for the broad "global news" (non-ticker) RSS top-up so the many
#: feeds are not re-fetched on every 15s refresh tick. The in-process RSS
#: cache already dedups network calls; this prevents redundant ingest work.
_GLOBAL_NEWS_INTERVAL = 600
_last_global_news_at: float = 0.0


def _db() -> Database:
    db = Database(settings.db_path)
    key = str(db.path.resolve())
    if key not in _initialized_dbs:
        db.init_schema()
        _initialized_dbs.add(key)
    return db


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MIN_PASSWORD = 8


def _set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        key="sv_session",
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
        path="/",
        max_age=auth.SESSION_TTL_HOURS * 3600,
    )


@app.post("/api/auth/register")
def auth_register(body: RegisterRequest):
    from . import auth

    db = _db()
    email = (body.email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="enter a valid email address")
    if not body.password or len(body.password) < _MIN_PASSWORD:
        raise HTTPException(status_code=422, detail=f"password must be at least {_MIN_PASSWORD} characters")
    if db.get_user_by_email(email):
        raise HTTPException(status_code=409, detail="an account with this email already exists")
    username = (body.username or "").strip()[:40]
    if not username:
        # Derive a friendly handle from the email local-part so the greeting
        # always has a name to address, even when the user skips the field.
        username = email.split("@")[0]
    user_id = secrets.token_hex(16)
    db.create_user(user_id, email, auth.hash_password(body.password), username)
    token = auth.new_session(db, user_id)
    resp = JSONResponse({"user": {"id": user_id, "email": email, "username": username}})
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/auth/login")
def auth_login(body: LoginRequest):
    from . import auth

    db = _db()
    user = db.get_user_by_email((body.email or "").strip())
    if not user or not auth.verify_password(body.password or "", user["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid email or password")
    token = auth.new_session(db, user["id"])
    resp = JSONResponse(
        {"user": {"id": user["id"], "email": user["email"], "username": user.get("username", "")}}
    )
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/auth/logout")
def auth_logout(sv_session: str | None = Cookie(default=None)):
    from . import auth

    auth.clear_session(_db(), sv_session)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("sv_session", path="/")
    return resp


@app.get("/api/auth/me")
def auth_me(user: dict = Depends(auth.current_user)):
    return {
        "user": {
            "id": user["id"],
            "email": user["email"],
            "username": user.get("username", ""),
        }
    }


def _analysis_context(
    db: Database, market: str | None = None
) -> tuple[list[dict[str, object]], dict[str, dict[str, object]], dict]:
    """Load stored verdicts + latest price snapshots + markets for analysis."""
    from .analysis import stock_analysis  # noqa: F401 (kept as the canonical builder)
    from .markets import load_markets

    rows = db.latest_verdicts(market=market)
    snaps = {
        f"{s['market']}:{s['ticker'].upper()}": s
        for s in db.latest_price_snapshots(market=market)
    }
    return rows, snaps, load_markets(settings.markets_dir)


@app.get("/api/search")
def search_tickers(
    q: str = "",
    market: str | None = None,
    limit: int = 10,
) -> list[dict[str, object]]:
    """Search the whole supported universe (configured + dynamically discovered).

    Configured 'featured' tickers rank first; results are enriched with the
    canonical stored analysis (committee verdict) when one already exists.
    """
    query = (q or "").strip()
    if not query:
        return []

    from . import instruments
    from .analysis import stock_analysis

    db = _db()
    rows, snaps, markets = _analysis_context(db, market)
    latest = {(r["market"], r["ticker"].upper()): r for r in rows}
    results = instruments.search_universe(query, limit=limit, market_filter=market)
    out: list[dict[str, object]] = []
    for item in results:
        row = latest.get((item.get("market"), (item.get("ticker") or "").upper()))
        if row:
            analysis = stock_analysis(
                row, snaps.get(f"{row['market']}:{row['ticker'].upper()}"), markets
            )
            out.append(
                {
                    **item,
                    "verdict": analysis["verdict"],
                    "confidence": analysis["confidence"],
                    "combined_score": analysis["combined_score"],
                    "reason": analysis["reason"],
                }
            )
        else:
            out.append(item)
    return out


def _dossier_target(
    symbol: str, market: str, ticker: str
) -> tuple[dict[str, object], str]:
    """Resolve a dossier request into (instrument, resolved_symbol) or raise 422/404."""
    from .universe import register

    db = _db()
    if symbol:
        from . import instruments

        item = instruments.resolve_symbol(symbol)
        if item is None:
            raise HTTPException(
                status_code=404, detail=f"Could not resolve symbol {symbol!r}"
            )
        full = item["symbol"]
        register(
            db,
            item.get("market"),
            item.get("ticker"),
            symbol=full,
            company=item.get("company") or "",
            exchange=item.get("exchange") or "",
            source="discovered",
        )
        return item, full

    if market and ticker:
        from .markets import load_markets
        from .resolve import resolve_for_fetch

        m = load_markets(settings.markets_dir).get(market.upper())
        if m is None:
            # Discovered market code (not in markets/*.json): resolve through the
            # stored registry so the Dossier still opens instead of a 422.
            from .universe import register, symbol_for

            full = symbol_for(db, market.upper(), ticker.upper())
            register(db, market.upper(), ticker.upper(), symbol=full, source="discovered")
            item: dict[str, object] = {
                "market": market.upper(),
                "ticker": ticker.upper(),
                "symbol": full,
                "company": "",
                "exchange": "",
                "quote_type": "EQUITY",
                "supported": True,
                "featured": False,
                "source": "discovered",
            }
            return item, full
        try:
            tkr = m.get_ticker(ticker)
            company = tkr.name or ""
            composed = f"{ticker.upper()}{tkr.yahoo_suffix or m.yahoo_suffix}"
            is_configured = True
        except KeyError:
            company = ""
            composed = f"{ticker.upper()}{m.yahoo_suffix}"
            is_configured = False
        # Prefer the resolver-validated symbol (handles ticker changes without
        # substituting a different security); fall back to the composed symbol
        # so stored-snapshot dossiers keep working.
        full = resolve_for_fetch(m.code, ticker.upper(), company) or composed
        register(
            db,
            m.code,
            ticker.upper(),
            symbol=full,
            company=company,
            exchange=m.name,
            currency=m.currency,
            source="configured" if is_configured else "discovered",
        )
        item: dict[str, object] = {
            "market": m.code,
            "ticker": ticker.upper(),
            "symbol": full,
            "company": company,
            "exchange": m.name,
            "quote_type": "EQUITY",
            "supported": True,
            "featured": ticker.upper() in m.tickers,
            "source": "local",
        }
        return item, full

    raise HTTPException(status_code=422, detail="Provide symbol OR market + ticker")


@app.get("/api/dossier")
def stock_dossier(
    symbol: str = "",
    market: str = "",
    ticker: str = "",
    fresh: bool = False,
) -> dict[str, object]:
    """Full stock dossier: verdict + committee + bull/bear + model + news + 13F.

    ``fresh=false`` (default) reuses a stored verdict when one exists (fast, no
    network); ``fresh=true`` runs the complete live pipeline via ``live_verdict``.
    """
    from . import institutional

    item, full = _dossier_target(symbol, market, ticker)
    if not item.get("supported"):
        raise HTTPException(
            status_code=422,
            detail=f"{item.get('symbol')} ({item.get('exchange') or 'unknown exchange'}) is not on a supported exchange",
        )

    mkt = item["market"]
    tkr = item["ticker"]
    db = _db()

    stored = None
    try:
        rows = db.latest_verdicts(market=mkt)
        stored = next((r for r in rows if r["ticker"].upper() == tkr.upper()), None)
    except Exception:
        stored = None

    verdict_dict: dict[str, object]
    computed_at = ""
    if fresh or stored is None:
        from .analysis import apply_canonical
        from .verdict import live_verdict

        try:
            # No forced symbol: live_verdict validates through the symbol
            # resolution layer before any price/LSTM work.
            v = live_verdict(mkt, tkr, item.get("company") or "")
        except Exception as exc:
            logger.exception("Dossier live verdict failed for %s", full)
            raise HTTPException(
                status_code=503, detail=f"Analysis failed for {full}: {exc}"
            )
        if v is None:
            detail = "no data available"
            try:
                from .resolve import resolution, status_label

                res = resolution(mkt, tkr, item.get("company") or "")
                note = res.get("note") or status_label(str(res.get("status")))
                if note:
                    detail = note
            except Exception:
                pass
            raise HTTPException(
                status_code=404,
                detail=f"Data unavailable: {detail}",
            )
        # A resolvable-but-unknown symbol (e.g. a delisted ticker) yields a
        # no-data verdict: treat it as not found rather than a bogus 200.
        if v.price is None and not v.news_available:
            raise HTTPException(
                status_code=404,
                detail=f"Data unavailable for {full}",
            )
        verdict_dict = v.as_dict()
        apply_canonical(verdict_dict)  # canonical committee verdict, consistent with scanner
        computed_at = utc_now()
        fresh = True
    else:
        from .analysis import (
            apply_canonical,
            snapshot_price,
            technical_from_snapshot,
            verdict_row_to_dict,
        )

        verdict_dict = verdict_row_to_dict(stored)
        snap = None
        try:
            snap = db.latest_price_snapshot(mkt, tkr)
        except Exception:
            snap = None
        price = snapshot_price(snap)
        if price is not None:
            verdict_dict["price"] = price
            technical_score, technical_reasons = technical_from_snapshot(snap)
            verdict_dict["technical"] = {
                "score": technical_score,
                "reasons": technical_reasons,
            }
        apply_canonical(verdict_dict)  # canonical committee verdict, consistent with scanner
        decided_at = stored.get("decided_at") or ""
        price_fetched_at = (snap or {}).get("fetched_at") or ""
        computed_at = max(decided_at, price_fetched_at) or decided_at

    institutional_data = institutional.ticker_institutional(tkr, db)

    # A result older than the slow (LSTM/news) refresh interval is considered
    # stale: the UI prioritizes re-analysis of the currently viewed stock and
    # shows STALE rather than presenting it as fresh.
    try:
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        stale = computed_at < now and (now[:19] > computed_at[:19]) and (
            (datetime.now(UTC) - datetime.fromisoformat(computed_at)).total_seconds()
            > settings.scanner_refresh_slow
        )
    except Exception:
        stale = False

    # Re-derive the structured decision with STALE awareness. Keep the committee
    # in its canonical signals shape (committee_signals, a list of signals) —
    # the UI renders it as a list, so it must never be replaced by the
    # committee_decision dict (whose "signals" is a key->signal map).
    from .dossier import committee_decision

    verdict_dict["decision"] = committee_decision(verdict_dict, stale=stale)

    from .markets import load_markets

    capabilities: dict[str, object] = {}
    market_cfg = load_markets(settings.markets_dir).get(mkt.upper())
    if market_cfg is not None:
        capabilities = market_cfg.as_dict()["capabilities"]

    return {
        "instrument": item,
        "verdict": verdict_dict,
        "committee": verdict_dict["committee"],
        "factors": verdict_dict["factors"],
        "institutional": institutional_data,
        "news": db.recent_news(mkt, tkr, limit=50),
        "computed_at": computed_at,
        "analyzed_at": computed_at,
        "stale": stale,
        "fresh": fresh,
        "capabilities": capabilities,
    }


@app.get("/api/scanner")
def scanner(
    verdict: str = "",
    market: str = "",
    signal_lstm: str = "",
    min_confidence: float = 0.0,
    min_momentum: float = -1.0,
    min_technical: float = -1.0,
    min_news: float = -1.0,
    sort: str = "combined",
    limit: int = 500,
) -> list[dict[str, object]]:
    """Scan the analyzed universe (stored verdicts + price snapshots) by signal.

    The universe is whatever has actually been scored by the pipeline — configured
    markets plus any dynamically searched/analyzed symbols — so it grows over time
    instead of being a hardcoded list, and there is no artificial ticker cap.
    Analysis is computed across the whole universe concurrently (Fincept-style),
    so large universes scan fast instead of stalling on a sequential loop.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from .analysis import stock_analysis, snapshot_price, technical_from_snapshot
    from .universe import universe

    db = _db()
    rows, snaps, markets = _analysis_context(db, market or None)
    latest = {(r["market"], r["ticker"].upper()): r for r in rows}

    def _analyze(sec: dict[str, object]) -> dict[str, object]:
        key = (sec["market"], sec["ticker"].upper())
        row = latest.get(key)
        snap = snaps.get(f"{key[0]}:{key[1]}")
        if row:
            analysis = stock_analysis(row, snap, markets)
            # Respect ready/stale freshness from the snapshot; do not blanket "ok".
            analysis["data_status"] = analysis.get("data_status", "ready")
            analysis["security"] = sec
            analysis["security_id"] = f"{key[0]}:{sec['ticker']}"
        elif snap:
            # Security has a price snapshot but no stored committee verdict: build a
            # real technical-based verdict from the snapshot (so the SCANNER shows an
            # actual BULL/BEAR/NEUTRAL call instead of a wall of N/A), and warm the
            # security in the background so it later gets the full quant/news verdict.
            tech_score, _ = technical_from_snapshot(snap)
            synth_row = {
                "market": sec["market"],
                "ticker": sec["ticker"],
                "verdict": "NEUTRAL",
                "confidence": 0.0,
                "news_score": 0.0,
                "price_score": 0.0,
                "combined_score": 0.0,
                "reason": "",
                "decided_at": (snap or {}).get("fetched_at") or "",
                "technical_score": tech_score,
                "lstm_score": 0.0,
                "lstm_probability_up": None,
                "lstm_predicted_return": None,
                "lstm_confidence": None,
                "signals": "",
            }
            analysis = stock_analysis(synth_row, snap, markets)
            analysis["security"] = sec
            analysis["security_id"] = f"{key[0]}:{sec['ticker']}"
            analysis["verdict_source"] = "technical_only"
            # Warm it so the full committee verdict (quant + news) replaces this.
            try:
                from . import refresh

                analysis["warming"] = bool(
                    refresh.is_warming(sec["market"], sec["ticker"])
                ) or bool(
                    refresh.enqueue_analysis(
                        str(db.path), sec["market"], sec["ticker"],
                        sec.get("company") or "", sec.get("symbol") or None,
                    )
                )
            except Exception:
                analysis["warming"] = False
        else:
            # Security is known but has no verdict yet. If a price snapshot exists,
            # surface its REAL price/technical data (READY or STALE) instead of
            # discarding it and showing N/A across the whole panel. The committee
            # verdict stays NO_DATA (per-metric), but the card is not blank.
            analysis = _no_data_analysis(sec, snap)
            # Warm it in the background so the next scan shows a real verdict.
            try:
                from . import refresh

                analysis["warming"] = bool(
                    refresh.is_warming(sec["market"], sec["ticker"])
                ) or bool(
                    refresh.enqueue_analysis(
                        str(db.path), sec["market"], sec["ticker"],
                        sec.get("company") or "", sec.get("symbol") or None,
                    )
                )
            except Exception:
                analysis["warming"] = False
        return analysis

    analyzed: list[dict[str, object]] = []
    secs = universe(db, market or None)
    with ThreadPoolExecutor(max_workers=min(32, max(8, len(secs)))) as pool:
        futures = [pool.submit(_analyze, sec) for sec in secs]
        for fut in as_completed(futures):
            try:
                analyzed.append(fut.result())
            except Exception as exc:  # noqa: BLE001 - one security never aborts the scan
                logger.warning("Scanner worker failed: %s", exc)

    out: list[dict[str, object]] = []
    for analysis in analyzed:
        if verdict and analysis["verdict"] != verdict.upper():
            continue
        if signal_lstm and analysis["lstm"]["signal"] != signal_lstm.upper():
            continue
        if min_confidence > 0 and (analysis["confidence"] or 0.0) < min_confidence:
            continue
        if min_technical > -1 and analysis["technical"]["score"] < min_technical:
            continue
        if min_news > -1 and (analysis["news"] or {}).get("score", -1.0) < min_news:
            continue
        if min_momentum > -1 and analysis["momentum_20"] < min_momentum:
            continue
        out.append(analysis)

    sort_keys = {
        "combined": lambda x: (x["combined_score"] is not None, x["combined_score"] or 0.0),
        "confidence": lambda x: (x["confidence"] is not None, x["confidence"] or 0.0),
        "momentum": lambda x: x["momentum_20"] if x["momentum_20"] is not None else 0.0,
        "prop_up": lambda x: x["lstm"]["probability_up"] or 0.0,
    }
    key_fn = sort_keys.get(sort, sort_keys["combined"])
    out.sort(key=key_fn, reverse=True)
    return out[:limit]


def _no_data_analysis(sec: dict[str, object], snap: dict[str, object] | None = None) -> dict[str, object]:
    """Minimal analysis entry for a known security with no verdict yet.

    If a price snapshot exists it is surfaced as REAL price/technical data (marked
    READY or STALE) — never fabricated — so the Scanner panel shows valid values
    instead of N/A. Per-metric states distinguish what is genuinely unavailable
    (committee/lstm/news -> no_data) from what is present.
    """
    price = snapshot_price(snap) if snap else None
    tech_score, tech_reasons = technical_from_snapshot(snap) if snap else (0.0, ["no price snapshot"])
    price_status = (price or {}).get("data_status", "no_data") if price else "no_data"
    close = (price or {}).get("close")
    data_status = price_status if price else "no_data"
    return {
        "market": sec.get("market"),
        "ticker": sec.get("ticker"),
        "symbol": sec.get("symbol") or sec.get("ticker"),
        "company": sec.get("company") or "",
        "verdict": "N/A",
        "confidence": None,
        "combined_score": None,
        "committee": {"verdict": "N/A", "score": None, "confidence": None, "signals": [], "why": ["no verdict yet"]},
        "factors": {"bull": [], "bear": []},
        "decision": None,
        "reason": ["NO_DATA — security has not been analyzed yet (price shown from snapshot if available)"],
        "decided_at": sec.get("last_analysis_at") or "",
        "price_fetched_at": (snap or {}).get("fetched_at") or "",
        "updated_at": (snap or {}).get("fetched_at") or sec.get("last_analysis_at") or "",
        "analyzed_at": (snap or {}).get("fetched_at") or sec.get("last_analysis_at") or "",
        "news_score": 0.0,
        "price_score": 0.0,
        "news_available": False,
        "signal_agreement": "unknown",
        "forecast_horizon": "",
        "lstm": {"score": 0.0, "probability_up": None, "predicted_return": None, "model_confidence": None, "metrics": {}, "model_version": "", "signal": "N/A"},
        "quantitative": {"status": "no_data"},
        "models": [],
        "social": None,
        "market_regime": None,
        "research": None,
        "technical": {"score": tech_score, "reasons": tech_reasons},
        "news": {"score": 0.0},
        "price": price,
        "price_status": price_status,
        "price_as_of": (price or {}).get("as_of", "") if price else "",
        "momentum_20": (price or {}).get("momentum_20", 0.0) if price else 0.0,
        "rsi_14": (price or {}).get("rsi_14", 50.0) if price else 50.0,
        "close": close,
        "above_sma_50": (price or {}).get("above_sma_50"),
        "data_status": data_status,
        "metrics_status": {
            "price": price_status,
            "technical": "ready" if price else "no_data",
            "committee": "no_data",
            "news": "no_data",
            "lstm": "no_data",
            "social": "no_data",
            "market_regime": "no_data",
        },
        "security": sec,
    }


@app.post("/api/refresh")
def refresh_data() -> dict[str, object]:
    """Run the background refresh cycle (fast price refresh + slow LSTM/news)."""
    from . import refresh

    db = _db()
    db.init_schema()
    result = refresh.run_refresh(db)

    # Top up the broad LIVE NEWS feed with the latest non-ticker headlines
    # (world / tech / crypto / macro). Throttled to the RSS cache TTL; all
    # feed fetches share an in-memory cache, so within 10 minutes this is a
    # cheap cache-hit pass over already-fetched articles.
    # When a dedicated worker runs (WORKER_MANAGED=1) the API skips this so
    # background work happens in exactly one process.
    global _last_global_news_at
    if settings.worker_managed:
        return result
    if time.time() - _last_global_news_at >= _GLOBAL_NEWS_INTERVAL:
        try:
            from .ingest import ingest_global_news

            ingest_global_news(str(db.path))
            _last_global_news_at = time.time()
        except Exception as exc:  # never let a news failure fail the refresh
            logger.warning("Global news top-up failed: %s", exc)

    return result


@app.get("/api/refresh/status")
def refresh_status() -> dict[str, object]:
    """Return the current background refresh status/timings."""
    from . import refresh

    return refresh.refresh_status()


# ---------------------------------------------------------------------------
# Paper research / portfolio (simulation only — no real orders)
# Fincept-style multi-portfolio engine (pt_*). portfolio_id is optional on
# read endpoints: when omitted, the user's default "Main" portfolio is used
# (preserving the legacy single-session UX).
# ---------------------------------------------------------------------------


def _paper_db() -> Database:
    db = _db()
    db.init_schema()
    return db


def _resolve_portfolio(db: Database, user_id: str, portfolio_id: str = "") -> str:
    from . import paper

    if portfolio_id:
        return portfolio_id
    return paper.ensure_default_portfolio(db, user_id)["id"]


@app.get("/api/paper/portfolios")
def paper_list_portfolios(user: dict = Depends(auth.current_user)) -> list[dict[str, object]]:
    from . import paper

    return paper.pt_list_portfolios(_paper_db(), user_id=user["id"])


@app.post("/api/paper/portfolios")
def paper_create_portfolio(
    body: PaperPortfolioCreate, user: dict = Depends(auth.current_user)
) -> dict[str, object]:
    from . import paper

    db = _paper_db()
    try:
        return paper.pt_create_portfolio(
            db, body.name, body.balance, user_id=user["id"], currency=body.currency,
            leverage=body.leverage, margin_mode=body.margin_mode, fee_rate=body.fee_rate,
            exchange=body.exchange,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.delete("/api/paper/portfolios/{portfolio_id}")
def paper_delete_portfolio(portfolio_id: str, user: dict = Depends(auth.current_user)) -> dict[str, object]:
    from . import paper

    db = _paper_db()
    try:
        paper.pt_delete_portfolio(db, portfolio_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"deleted": portfolio_id}


@app.post("/api/paper/portfolios/{portfolio_id}/reset")
def paper_reset_portfolio(portfolio_id: str, user: dict = Depends(auth.current_user)) -> dict[str, object]:
    from . import paper

    db = _paper_db()
    try:
        return paper.pt_reset_portfolio(db, portfolio_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/paper/portfolios/{portfolio_id}/balance")
def paper_set_balance(
    portfolio_id: str, body: dict[str, float], user: dict = Depends(auth.current_user)
) -> dict[str, object]:
    from . import paper

    db = _paper_db()
    try:
        paper.pt_set_balance(db, portfolio_id, float(body.get("balance", 0.0)))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return paper.pt_get_portfolio(db, portfolio_id)


@app.post("/api/paper/portfolios/{portfolio_id}/market-hours")
def paper_set_market_hours(
    portfolio_id: str, body: dict[str, bool], user: dict = Depends(auth.current_user)
) -> dict[str, object]:
    from . import paper

    db = _paper_db()
    paper.pt_set_enforce_market_hours(db, portfolio_id, bool(body.get("enforce", False)))
    return paper.pt_get_portfolio(db, portfolio_id)


@app.get("/api/paper/portfolio")
def paper_portfolio(
    portfolio_id: str = "", user: dict = Depends(auth.current_user)
) -> dict[str, object]:
    from . import paper

    db = _paper_db()
    pid = _resolve_portfolio(db, user["id"], portfolio_id)
    return paper.pt_portfolio_state(db, pid)


@app.get("/api/paper/quote")
def paper_quote(market: str, ticker: str, user: dict = Depends(auth.current_user)) -> dict[str, object]:
    from . import paper

    return paper.pt_quote(_paper_db(), market, ticker)


@app.post("/api/paper/order")
def paper_order(body: PaperOrder, user: dict = Depends(auth.current_user)) -> dict[str, object]:
    from . import paper

    db = _paper_db()
    pid = _resolve_portfolio(db, user["id"], body.portfolio_id)
    try:
        order = paper.pt_place_order(
            db, pid, body.market, body.ticker, body.side, body.order_type, body.quantity,
            price=body.price, stop_price=body.stop_price, reduce_only=body.reduce_only,
            product=body.product, exchange=body.exchange,
            decision_id=body.decision_id or None, reason=body.reason or "", user_id=user["id"],
        )
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    # Portfolio <-> watchlist sync: anything added to the portfolio (any paper
    # trade) is automatically tracked on the watchlist so it shows up in the
    # overview WATCHLIST rail and the portfolio TRACKED SECURITIES table.
    try:
        if db.add_to_watchlist(body.market, body.ticker):
            from .universe import register

            register(db, body.market, body.ticker, source="portfolio")
    except Exception as exc:  # pragma: no cover - sync must never block a fill
        logger.warning("Watchlist sync failed for %s:%s: %s", body.market, body.ticker, exc)
    return {"order": order, "portfolio": paper.pt_portfolio_state(db, pid)}


@app.post("/api/paper/orders/{order_id}/cancel")
def paper_cancel_order(order_id: str, user: dict = Depends(auth.current_user)) -> dict[str, object]:
    from . import paper

    db = _paper_db()
    try:
        paper.pt_cancel_order(db, order_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"cancelled": order_id}


@app.get("/api/paper/orders")
def paper_orders(
    portfolio_id: str = "", status: str = "", user: dict = Depends(auth.current_user)
) -> list[dict[str, object]]:
    from . import paper

    db = _paper_db()
    pid = _resolve_portfolio(db, user["id"], portfolio_id)
    return paper.pt_get_orders(db, pid, status)


@app.get("/api/paper/positions")
def paper_positions(
    portfolio_id: str = "", user: dict = Depends(auth.current_user)
) -> list[dict[str, object]]:
    from . import paper

    db = _paper_db()
    pid = _resolve_portfolio(db, user["id"], portfolio_id)
    return paper.pt_get_positions(db, pid)


@app.post("/api/paper/positions/{position_id}/convert")
def paper_convert_position(
    position_id: str, body: dict[str, str], user: dict = Depends(auth.current_user)
) -> dict[str, object]:
    from . import paper

    db = _paper_db()
    try:
        paper.pt_convert_position_product(db, position_id, str(body.get("product", "")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    pos = db.pt_get_position(position_id)
    return pos


@app.get("/api/paper/trades")
def paper_trades(
    portfolio_id: str = "", limit: int = 100, user: dict = Depends(auth.current_user)
) -> list[dict[str, object]]:
    from . import paper

    db = _paper_db()
    pid = _resolve_portfolio(db, user["id"], portfolio_id)
    return paper.pt_get_trades(db, pid, limit)


@app.get("/api/paper/stats")
def paper_stats(
    portfolio_id: str = "", user: dict = Depends(auth.current_user)
) -> dict[str, object]:
    from . import paper

    db = _paper_db()
    pid = _resolve_portfolio(db, user["id"], portfolio_id)
    return paper.pt_get_stats(db, pid)


@app.get("/api/paper/risk")
def paper_risk(
    portfolio_id: str = "", user: dict = Depends(auth.current_user)
) -> dict[str, object]:
    from . import paper

    db = _paper_db()
    pid = _resolve_portfolio(db, user["id"], portfolio_id)
    return paper.pt_risk(db, pid)


@app.get("/api/paper/equity")
def paper_equity(
    portfolio_id: str = "", user: dict = Depends(auth.current_user)
) -> list[dict[str, object]]:
    from . import paper

    db = _paper_db()
    pid = _resolve_portfolio(db, user["id"], portfolio_id)
    return paper.pt_equity_history(db, pid)


@app.post("/api/paper/end-session")
def paper_end_session(
    portfolio_id: str = "", user: dict = Depends(auth.current_user)
) -> dict[str, object]:
    from . import paper

    db = _paper_db()
    pid = _resolve_portfolio(db, user["id"], portfolio_id)
    return paper.pt_end_session(db, pid)


@app.post("/api/paper/settle")
def paper_settle(
    portfolio_id: str = "", user: dict = Depends(auth.current_user)
) -> dict[str, object]:
    from . import paper

    db = _paper_db()
    if portfolio_id:
        n = paper.pt_settle_intraday(db, portfolio_id)
    else:
        n = paper.pt_settle_intraday_all(db)
    return {"squared_off": n}


@app.get("/api/paper/leaderboard")
def paper_leaderboard(
    portfolio_id: str = "", user: dict = Depends(auth.current_user)
) -> dict[str, object]:
    from . import paper

    db = _paper_db()
    pid = _resolve_portfolio(db, user["id"], portfolio_id)
    return paper.pt_leaderboard(db, pid)


@app.get("/api/paper/decisions")
def paper_decisions(
    market: str | None = None,
    ticker: str | None = None,
    user: dict = Depends(auth.current_user),
) -> list[dict[str, object]]:
    db = _paper_db()
    return db.decision_snapshots(market=market, ticker=ticker)


@app.get("/api/paper/performance")
def paper_performance(user: dict = Depends(auth.current_user)) -> dict[str, object]:
    from . import paper

    return paper.performance(_paper_db())


@app.post("/api/paper/evaluate")
def paper_evaluate(force: bool = False, user: dict = Depends(auth.current_user)) -> dict[str, object]:
    from . import paper

    return paper.refresh_evaluations(_paper_db(), force=force)


@app.post("/api/simulate")
def simulate(body: ReplayRequest) -> dict[str, object]:
    """Chronological historical replay (isolated; never touches the paper portfolio)."""
    from . import replay

    db = _db()
    db.init_schema()
    return replay.run(
        db,
        body.market,
        body.ticker,
        body.start_date,
        body.end_date,
        timeframe=body.timeframe,
        decision_interval=body.decision_interval,
        capital=body.capital,
        bull_threshold=body.bull_threshold,
        bear_threshold=body.bear_threshold,
        size_ratio=body.size_ratio,
    )


@app.get("/api/markets")
def list_markets() -> list[dict[str, object]]:
    from datetime import UTC, datetime

    from .markets import load_markets, market_status

    markets = load_markets(settings.markets_dir)
    now = datetime.now(UTC)
    out = []
    for m in markets.values():
        item = m.as_dict()
        item["status"] = market_status(m, now)
        out.append(item)
    return out


@app.get("/api/analytics/committee")
def committee_analytics(min_sample: int = 5) -> dict[str, object]:
    """Committee performance analytics from stored decision snapshots.

    Directional / per-verdict accuracy, conviction-vs-accuracy, forecast error,
    and breakdowns by market, signal and regime. Every bucket carries its
    sample size n; buckets below ``min_sample`` are flagged reliable=false.
    """
    from .analytics import committee_analytics

    return committee_analytics(_db(), min_sample=max(1, int(min_sample)))


@app.get("/api/health/data")
def data_health() -> dict[str, object]:
    """Lightweight data-health visibility: providers, stale/NO_DATA/ERROR counts,
    signal + market coverage, last refresh, worker/job health."""
    from .analytics import data_health as _data_health

    return _data_health(_db())


@app.get("/api/notifications")
def get_notifications(limit: int = 50) -> list[dict[str, object]]:
    from . import notifications

    return notifications.recent(_db(), limit=limit)


@app.post("/api/notifications/scan")
def scan_notifications() -> dict[str, object]:
    """Run the event detectors (market open, committee changes, significant
    trades). Deterministic event keys make repeated polls idempotent."""
    from . import notifications

    return notifications.scan(_db())


@app.post("/api/notifications/ack")
def ack_notifications(body: AckRequest) -> dict[str, object]:
    from . import notifications

    return notifications.ack(_db(), body.keys)


def _price_alert_view(db: Database, rule: dict[str, object]) -> dict[str, object]:
    """Attach the latest stored quote and distance-to-target to an alert rule."""
    item = dict(rule)
    item["active"] = bool(item.get("active"))
    snapshot = db.latest_price_snapshot(str(item["market"]), str(item["ticker"]))
    current = float(snapshot.get("close") or 0.0) if snapshot else 0.0
    target = float(item.get("target_price") or 0.0)
    item["current_price"] = current or None
    item["price_as_of"] = snapshot.get("as_of") or snapshot.get("fetched_at") if snapshot else None
    item["data_status"] = snapshot.get("data_status") or "ready" if snapshot else "no_data"
    item["distance_pct"] = ((target - current) / current) if current > 0 else None
    return item


@app.get("/api/price-alerts")
def get_price_alerts() -> list[dict[str, object]]:
    db = _db()
    return [_price_alert_view(db, rule) for rule in db.price_alerts()]


@app.post("/api/price-alerts")
def create_price_alert(body: PriceAlertCreate) -> dict[str, object]:
    market = (body.market or "").strip().upper()
    ticker = (body.ticker or "").strip().upper()
    direction = (body.direction or "").strip().lower()
    if not market or not ticker:
        raise HTTPException(status_code=422, detail="market and ticker are required")
    if direction not in {"above", "below"}:
        raise HTTPException(status_code=422, detail="direction must be above or below")
    if not 0 < body.target_price < 1_000_000_000:
        raise HTTPException(status_code=422, detail="target price must be greater than zero")
    db = _db()
    rule = db.create_price_alert(
        market, ticker, direction, body.target_price, body.note,
    )
    # Alerted names join the canonical universe so normal refreshes keep their
    # latest price current even when they are not on the watchlist.
    try:
        from .universe import register

        register(db, market, ticker, source="price_alert")
    except Exception:
        pass
    return _price_alert_view(db, rule)


@app.patch("/api/price-alerts/{alert_id}")
def update_price_alert(alert_id: int, body: PriceAlertUpdate) -> dict[str, object]:
    db = _db()
    rule = db.update_price_alert(alert_id, active=body.active)
    if rule is None:
        raise HTTPException(status_code=404, detail="price alert not found")
    return _price_alert_view(db, rule)


@app.delete("/api/price-alerts/{alert_id}")
def delete_price_alert(alert_id: int) -> dict[str, object]:
    removed = _db().delete_price_alert(alert_id)
    if not removed:
        raise HTTPException(status_code=404, detail="price alert not found")
    return {"removed": True, "id": alert_id}


def _news_importance(score: Any, label: Any) -> str:
    """Map a news sentiment score to a coarse importance band (real data only)."""
    try:
        magnitude = abs(float(score))
    except (TypeError, ValueError):
        magnitude = 0.0
    if magnitude >= 0.5:
        return "HIGH"
    if magnitude >= 0.25:
        return "IMPORTANT"
    return "INFO"


@app.get("/api/events")
def get_events(limit: int = 40) -> list[dict[str, object]]:
    """Live market event feed: real news headlines + terminal events, ranked.

    News items come straight from stored articles (never fabricated); terminal
    events reuse the existing notification stream (market open, committee change,
    significant trade). Ranked by importance then recency.
    """
    from . import notifications

    db = _db()
    events: list[dict[str, object]] = []
    for n in db.recent_news_feed(limit=limit):
        events.append({
            "id": f"news:{n['market']}:{n['ticker']}:{n.get('published_at') or n.get('fetched_at')}",
            "timestamp": n.get("published_at") or n.get("fetched_at") or "",
            "security_id": n["security_id"],
            "market": n["market"],
            "ticker": n["ticker"],
            "headline": n["title"],
            "source": n.get("source") or "NEWS",
            "type": "news",
            "sentiment": n.get("sentiment_label") or "",
            "importance": _news_importance(n.get("sentiment_score"), n.get("sentiment_label")),
        })
    for a in notifications.recent(db, limit=limit):
        events.append({
            "id": a["event_key"],
            "timestamp": a.get("created_at") or "",
            "security_id": a.get("security_id") or "",
            "market": a.get("market") or "",
            "ticker": a.get("ticker") or "",
            "headline": a.get("title") or a.get("message") or "",
            "source": "TERMINAL",
            "type": a.get("type") or "event",
            "sentiment": "",
            "importance": a.get("severity") or "INFO",
        })

    rank = {"HIGH": 3, "IMPORTANT": 2, "INFO": 1, "": 0}
    events.sort(key=lambda e: (rank.get(str(e["importance"]), 0), e["timestamp"]), reverse=True)
    return events[:limit]


@app.get("/api/screener")
def screener(
    market: str | None = None,
    q: str = "",
    sector: str = "",
    preset: str = "",
    verdict: str = "",
    min_conviction: float = 0.0,
    min_momentum: float | None = None,
    max_momentum: float | None = None,
    min_move: float | None = None,
    min_volume_ratio: float | None = None,
    signal: str = "",
    signal_key: str = "quant",
    min_agreement: float | None = None,
    regime: str = "",
    above_sma: str = "",
    news_min: float | None = None,
    research: str = "",
    reversal: str = "",
    conflict: str = "",
    no_data_only: bool = False,
    sort: str = "combined",
    limit: int = 100,
) -> list[dict[str, object]]:
    """Screen the dynamic universe (configured/discovered securities, never a
    hardcoded list). Results reuse the canonical analysis + Committee data."""
    from . import screener as screener_mod

    filters = dict(
        market=market, q=q, sector=sector, verdict=verdict,
        min_conviction=min_conviction, min_momentum=min_momentum,
        max_momentum=max_momentum, min_move=min_move,
        min_volume_ratio=min_volume_ratio, signal=signal, signal_key=signal_key,
        min_agreement=min_agreement, regime=regime, above_sma=above_sma,
        news_min=news_min, research=research, reversal=reversal,
        conflict=conflict, no_data_only=no_data_only, sort=sort, limit=limit,
    )
    if preset:
        filters.update(screener_mod.apply_preset(preset))
    return screener_mod.run(_db(), **filters)


@app.get("/api/verdicts")
def get_verdicts(
    market: str | None = None,
    ticker: str | None = None,
    live: bool = False,
) -> dict[str, dict[str, object]]:
    db = _db()
    if live:
        from .verdict import run_verdicts

        market_codes = [market] if market else None
        verdicts = run_verdicts(market_codes=market_codes)
        if ticker:
            verdicts = {
                k: v for k, v in verdicts.items() if v.ticker.upper() == ticker.upper()
            }
        return {k: v.as_dict() for k, v in verdicts.items()}

    from .analysis import stock_analysis

    rows, snaps, markets = _analysis_context(db, market)
    if ticker:
        rows = [r for r in rows if r["ticker"].upper() == ticker.upper()]
    return {
        f"{r['market']}:{r['ticker']}": stock_analysis(
            r, snaps.get(f"{r['market']}:{r['ticker'].upper()}"), markets
        )
        for r in rows
    }


@app.get("/api/news")
def get_news(
    market: str, ticker: str, limit: int = 100, refresh: bool = False
) -> list[dict[str, object]]:
    """News for a single stock.

    With ``refresh=true`` the backend performs a live Google News / Yahoo fetch
    for that ticker, stores any new articles, then returns the combined, most
    recent coverage for the stock (never fabricated).
    """
    db = _db()
    if refresh:
        try:
            from .ingest import run_ticker_ingest

            run_ticker_ingest(market, ticker)
        except Exception as exc:  # pragma: no cover - network/parse safety
            logger.warning("Live ticker ingest failed for %s:%s: %s", market, ticker, exc)
    return db.recent_news(market, ticker, limit=min(limit, 500))


@app.get("/api/news/feed")
def get_news_feed(limit: int = 200) -> list[dict[str, object]]:
    """Global news feed across all tickers, with article URLs + sentiment.

    Returns the most recent articles (never fabricated) so the NEWS tab can
    render a clickable feed that opens the source article.
    """
    db = _db()
    return db.recent_news_feed(limit=min(limit, 1000))


@app.get("/api/watchlist")
def get_watchlist() -> list[dict[str, object]]:
    from .analysis import stock_analysis

    db = _db()
    db.init_schema()
    rows, snaps, markets = _analysis_context(db)
    latest = {(r["market"], r["ticker"].upper()): r for r in rows}
    out: list[dict[str, object]] = []
    for w in db.watchlist():
        item: dict[str, object] = {
            "market": w["market"],
            "ticker": w["ticker"],
            "company": w["company"],
            "added_at": w["added_at"],
        }
        row = latest.get((w["market"], w["ticker"].upper()))
        if row:
            analysis = stock_analysis(
                row, snaps.get(f"{row['market']}:{row['ticker'].upper()}"), markets
            )
            item["verdict"] = analysis["verdict"]
            item["confidence"] = analysis["confidence"]
            item["news_score"] = analysis["news_score"]
            item["price_score"] = analysis["price_score"]
            item["combined_score"] = analysis["combined_score"]
            item["reason"] = analysis["reason"]
            item["decided_at"] = analysis["decided_at"]
        out.append(item)
    return out


@app.post("/api/watchlist")
def add_watchlist(item: WatchItem, analyze: bool = True) -> dict[str, object]:
    if not item.market or not item.ticker:
        raise HTTPException(status_code=422, detail="market and ticker are required")
    db = _db()
    db.init_schema()
    added = db.add_to_watchlist(item.market, item.ticker, item.company)
    response: dict[str, object] = {
        "added": added,
        "market": item.market.upper(),
        "ticker": item.ticker.upper(),
    }
    # Register in the canonical universe so the security is immediately
    # discoverable; the background refresh performs the analysis.
    try:
        from .universe import register

        register(db, item.market, item.ticker, company=item.company, source="watchlist")
    except Exception:
        pass
    if added and analyze:
        from .verdict import live_verdict

        v = live_verdict(item.market, item.ticker, item.company)
        if v is not None:
            response["verdict"] = v.as_dict()
    return response


@app.delete("/api/watchlist")
def delete_watchlist(market: str, ticker: str) -> dict[str, object]:
    db = _db()
    removed = db.remove_from_watchlist(market, ticker)
    return {"removed": removed, "market": market.upper(), "ticker": ticker.upper()}


# ---------------------------------------------------------------------------
# Strategy Agent discovery
# ---------------------------------------------------------------------------


class AgentWorkflowRequest(BaseModel):
    prompt: str = ""
    market: str | None = None
    limit: int = 30
    criteria: dict[str, object] | None = None


@app.post("/api/agent/workflow")
def agent_workflow(req: AgentWorkflowRequest) -> dict[str, object]:
    """Agent Workflow screening.

    This IS the strategy-discovery path: the Agent prompts the universe and returns
    REAL qualifying securities (each with a canonical ``market:ticker`` id), ranked
    by a composite of available signals, plus a separate NOT_EVALUABLE list for
    names that could not be judged. No separate strategy engine/page/db exists.
    """
    from .agent_tools import screen_workflow

    return screen_workflow(
        prompt=req.prompt or None,
        market=req.market,
        limit=req.limit,
        criteria=req.criteria,
    )


# ---------------------------------------------------------------------------
# Portfolio Groups
# ---------------------------------------------------------------------------


class PortfolioGroupCreate(BaseModel):
    name: str
    description: str = ""
    #: Origin of the group. "agent_workflow" marks groups created from Agent results
    #: (static snapshot). Plain metadata — never auto-rebalanced or rerun.
    source: str = "manual"
    workflow_text: str | None = None
    members: list[dict[str, str]] = []


class PortfolioGroupRename(BaseModel):
    name: str
    description: str | None = None


class PortfolioGroupAdd(BaseModel):
    market: str
    ticker: str


@app.get("/api/portfolio/groups")
def get_portfolio_groups() -> list[dict[str, object]]:
    return _db().list_groups()


@app.post("/api/portfolio/groups")
def create_portfolio_group(req: PortfolioGroupCreate) -> dict[str, object]:
    if not req.name:
        raise HTTPException(status_code=422, detail="group name is required")
    db = _db()
    import uuid

    gid = f"grp_{uuid.uuid4().hex[:12]}"
    # The workflow text (if any) is preserved as metadata in the strategy_name
    # column so no new schema is required.
    group = db.create_group(
        gid,
        req.name,
        req.description,
        source=req.source,
        strategy_name=req.workflow_text,
    )
    for m in req.members:
        if m.get("market") and m.get("ticker"):
            db.add_to_group(gid, m["market"], m["ticker"])
    return db.get_group(gid)


@app.patch("/api/portfolio/groups/{group_id}")
def rename_portfolio_group(group_id: str, req: PortfolioGroupRename) -> dict[str, object]:
    group = _db().rename_group(group_id, req.name, req.description)
    if not group:
        raise HTTPException(status_code=404, detail="group not found")
    return group


@app.delete("/api/portfolio/groups/{group_id}")
def delete_portfolio_group(group_id: str) -> dict[str, object]:
    removed = _db().delete_group(group_id)
    return {"removed": removed, "group_id": group_id}


@app.post("/api/portfolio/groups/{group_id}/members")
def add_portfolio_group_member(group_id: str, req: PortfolioGroupAdd) -> dict[str, object]:
    added = _db().add_to_group(group_id, req.market, req.ticker)
    return {"added": added, "group_id": group_id, "market": req.market.upper(), "ticker": req.ticker.upper()}


@app.delete("/api/portfolio/groups/{group_id}/members")
def remove_portfolio_group_member(group_id: str, market: str, ticker: str) -> dict[str, object]:
    removed = _db().remove_from_group(group_id, market, ticker)
    return {"removed": removed, "group_id": group_id, "market": market.upper(), "ticker": ticker.upper()}


@app.get("/api/agent")
def agent_recommendations(
    market: str | None = None,
    live: bool = False,
    provider: str = "gemini",
    model: str | None = None,
) -> dict[str, object]:
    """Return LLM trading recommendations.

    When live=1, orchestrates fresh data and asks the LLM now (may take a while).
    Otherwise returns the latest persisted recommendations.
    """
    db = _db()
    db.init_schema()
    if live:
        from .agent import run_agent

        try:
            market_codes = [market] if market else None
            recs = run_agent(market_codes=market_codes, provider=provider, model=model)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        items = [r.as_dict() for r in recs]
        latest = db.latest_recommendations(market=market)
        return {
            "live": True,
            "generated_at": latest[-1]["generated_at"] if latest else "",
            "recommendations": items,
        }

    rows = db.latest_recommendations(market=market)
    generated = rows[-1]["generated_at"] if rows else ""
    return {
        "live": False,
        "generated_at": generated,
        "recommendations": [
            {
                "market": r["market"],
                "ticker": r["ticker"],
                "company": r["company"],
                "action": r["action"],
                "confidence": r["confidence"],
                "rationale": r["rationale"],
            }
            for r in rows
        ],
    }


class AgentChatRequest(BaseModel):
    messages: list[dict] = []
    market: str | None = None
    mode: str = "AUTO"
    provider: str = "auto"
    model: str = ""
    search: str = ""  # '' default | 'deep' (deep search) | 'low' (low-token search)


@app.post("/api/agent/chat")
def agent_chat(body: AgentChatRequest) -> dict[str, object]:
    """Conversational markets assistant.

    Routes to the requested LLM provider (auto / gemini / ollama / local).
    ``local`` always uses the built-in data-driven responder; ``auto`` uses any
    configured LLM and falls back to local on failure. ``search`` selects the
    agent's search style: deep (exhaustive, multi-tool) or low (low-token).
    The used provider and live provider availability are returned so the UI can
    show status.
    """
    from .agent_chat import chat as agent_chat_fn

    return agent_chat_fn(
        body.messages,
        market=body.market,
        mode=body.mode,
        provider=body.provider,
        model=body.model,
        search=body.search,
    )


@app.get("/api/agent/config")
def agent_config() -> dict[str, object]:
    """Available agent providers + whether their keys/servers are configured."""
    from .agent_chat import config as agent_config

    return agent_config()


@app.get("/api/analyze")
def analyze_ticker(
    market: str,
    ticker: str,
    company: str = "",
    provider: str = "gemini",
    model: str | None = None,
) -> dict[str, object]:
    """Deep-dive LLM analysis for a single, user-selected ticker."""
    if not market or not ticker:
        raise HTTPException(status_code=422, detail="market and ticker are required")
    from .agent import run_agent_analysis

    try:
        analysis = run_agent_analysis(
            market_code=market,
            ticker=ticker,
            company=company,
            provider=provider,
            model=model,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return analysis.as_dict()


@app.get("/api/reddit")
def reddit_recommendations(
    subreddits: str = "",
    limit: int = 50,
    time_filter: str = "day",
    min_mentions: int = 2,
    min_score: int = 10,
) -> dict[str, object]:
    """Reddit stock recommendations from subreddit scanning."""
    sub_list = (
        [s.strip() for s in subreddits.split(",") if s.strip()] if subreddits else None
    )
    from .reddit_scanner import run_reddit_scan

    try:
        recs = run_reddit_scan(
            subreddits=sub_list,
            limit_per_sub=limit,
            time_filter=time_filter,
            min_mentions=min_mentions,
            min_score=min_score,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"recommendations": [r.as_dict() for r in recs]}


@app.get("/api/lstm/batch-predict")
def lstm_batch_predict(
    symbols: str,
    period: str = "2y",
    window: int = 30,
) -> dict[str, object]:
    """Get LSTM price predictions for multiple symbols."""
    from .models.price_lstm import batch_predict_lstm

    symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    results = batch_predict_lstm(symbol_list, period=period, window=window)
    return {k: v.as_dict() for k, v in results.items()}


@app.get("/api/lstm/train")
def lstm_train(
    symbol: str,
    period: str = "2y",
    window: int = 30,
    epochs: int = 25,
    batch_size: int = 32,
    lr: float = 1e-3,
) -> dict[str, object]:
    """Train the SINGLE shared (global) LSTM over the universe and return a
    prediction for ``symbol`` using it.

    We no longer train one model per ticker (that created hundreds of checkpoint
    files and re-loaded a model per request). ``train_price_lstm`` trains one
    cross-asset model and reuses it for every symbol.
    """
    from .models.price_lstm import train_price_lstm

    res = train_price_lstm(
        symbol,
        period=period,
        window=window,
        epochs=epochs,
        batch_size=batch_size,
        lr=lr,
    )
    if res is None:
        raise HTTPException(status_code=404, detail=f"Could not train global model (no price data for {symbol})")
    return res.as_dict()


@app.get("/api/funds")
def hedge_funds() -> list[dict[str, object]]:
    """Latest 13F summary for each tracked hedge fund."""
    from .institutional import fund_summaries

    return fund_summaries(_db())


@app.get("/api/funds/refresh")
def refresh_hedge_funds() -> dict[str, object]:
    """Fetch the latest 13F filings from SEC EDGAR for all tracked funds."""
    from .institutional import fund_summaries, run_institutional_fetch

    filings = run_institutional_fetch()
    return {
        "fetched": [f.fund_name for f in filings],
        "summaries": fund_summaries(_db()),
    }


@app.get("/api/funds/{cik}")
def hedge_fund_detail(cik: str) -> dict[str, object]:
    """Detailed holdings + quarterly changes for one fund."""
    from .institutional import compute_quarterly_changes

    db = _db()
    db.init_schema()
    filings = db.fund_filings(cik=cik, limit=2)
    if not filings:
        raise HTTPException(status_code=404, detail="fund not found")
    latest = filings[0]
    changes = compute_quarterly_changes(cik, db)
    return {
        "fund_id": latest["cik"],
        "cik": latest["cik"],
        "fund": latest["fund_name"],
        "manager": latest["fund_name"],
        "form": latest["form"],
        "filing_date": latest["filing_date"],
        "period_of_report": latest["period_of_report"],
        "holdings": [
            {
                "cusip": h["cusip"],
                "issuer": h["issuer"],
                "ticker": h["ticker"],
                "value": h["value_thousands"],
                "shares": h["shares"],
                "shares_type": h["shares_type"],
                "put_call": h["put_call"],
                "pct_portfolio": h["pct_portfolio"],
            }
            for h in db.fund_holdings(latest["id"], limit=500)
        ],
        "changes": [
            {
                "ticker": c.ticker,
                "issuer": c.issuer,
                "action": c.action,
                "prev_shares": c.prev_shares,
                "curr_shares": c.curr_shares,
                "change_shares": c.change_shares,
                "change_pct": round(c.change_pct, 4),
                "value": c.value_thousands,
            }
            for c in changes
        ],
    }


@app.get("/api/indexes")
def get_indexes(market: str | None = None) -> list[dict[str, object]]:
    """Latest index snapshots, optionally filtered by market."""
    db = _db()
    db.init_schema()
    snapshots = db.latest_index_snapshots(market=market)
    return [
        {
            "market": s["market"],
            "symbol": s["symbol"],
            "name": s["name"],
            "close": s["close"],
            "open": s["open"],
            "high": s["high"],
            "low": s["low"],
            "volume": s["volume"],
            "change_pct": s["change_pct"],
            "fetched_at": s["fetched_at"],
        }
        for s in snapshots
    ]


@app.get("/api/ticker-strip")
def ticker_strip(market: str | None = None, limit: int = 500) -> list[dict[str, object]]:
    """Real security universe with most-recent market data, for the overhead strip.

    Only securities that actually have stored price data are returned (securities
    without data are omitted rather than fabricated). Prices/change come from the
    latest stored snapshots; ``change_pct`` is ``None`` when a prior snapshot is
    unavailable so the UI can show NO_DATA instead of an invented move.
    """
    db = _db()
    db.init_schema()
    rows = db.ticker_strip_snapshots(limit=limit, market=market)
    return [
        {
            "security_id": f"{r['market']}:{r['ticker']}",
            "market": r["market"],
            "ticker": r["ticker"],
            "company": r.get("company") or "",
            "exchange": r.get("exchange") or "",
            "currency": r.get("currency") or "",
            "close": r["close"],
            "change_pct": (round(r["change_pct"], 4) if r["change_pct"] is not None else None),
            "price_date": r.get("fetched_at") or "",
        }
        for r in rows
    ]


@app.get("/api/indexes/refresh")
def refresh_indexes(market: str | None = None) -> list[dict[str, object]]:
    """Fetch fresh index snapshots and return them."""
    from .indexes import run_index_fetch

    codes = [market] if market else None
    snapshots = run_index_fetch(codes)
    return [s.as_dict() for s in snapshots]


@app.get("/api/indexes/{symbol}/history")
def index_history(symbol: str, range: str = "1mo") -> dict[str, object]:
    """OHLC history for an index at a given range (1d/1w/1mo/1y/all)."""
    from .indexes import index_history as fetch_history

    rows = fetch_history(symbol, range)
    return {"symbol": symbol, "range": range, "data": rows}


@app.get("/api/chart/{market}/{ticker}")
def chart_data(
    market: str, ticker: str, range: str = "1mo", symbol: str = ""
) -> dict[str, object]:
    """Stock chart OHLC data for any ticker at a given range (1d/1w/1mo/1y/all).

    ``symbol`` optionally overrides the provider symbol (exact resolution from
    dynamic symbol discovery); otherwise it is composed from market + suffix.
    """
    from .indexes import index_history as fetch_history
    from .markets import load_markets

    if symbol:
        sym = symbol.upper()
    else:
        markets = load_markets(settings.markets_dir)
        m = markets.get(market.upper())
        suffix = m.yahoo_suffix if m else ""
        sym = f"{ticker.upper()}{suffix}"
    rows = fetch_history(sym, range)
    return {
        "market": market.upper(),
        "ticker": ticker.upper(),
        "symbol": sym,
        "range": range,
        "data": rows,
    }


@app.get("/", include_in_schema=False)
def index():
    assets = _ui_assets_dir()
    f = assets / "index.html"
    if f.is_file():
        return FileResponse(f)
    return HTMLResponse(
        "<html><body style='background:#000;color:#f5a623;font-family:monospace'>"
        "STOCKVERDICT — frontend not built. Run <code>npm run build</code> in "
        "<code>frontend/</code>, then restart.</body></html>",
        status_code=503,
    )


# SPA fallback: serve the built React app from frontend/dist; any unmatched path
# falls back to index.html so client-side routes work.
if (FRONTEND_DIR / "index.html").is_file():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        # Serve only files that actually live inside the dist directory —
        # never allow ".." traversal outside of it.
        candidate = (FRONTEND_DIR / full_path).resolve()
        try:
            candidate.relative_to(FRONTEND_DIR.resolve())
        except ValueError:
            return JSONResponse({"detail": "not found"}, status_code=404)
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIR / "index.html")
