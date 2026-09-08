"""Paper research engine — decision snapshots, historical evaluation, and a
Fincept-style multi-portfolio simulated trading engine.

PAPER TRADING ONLY. No broker integration, no real orders, no real money.
Every order is a simulation recorded in the local database.

The trading engine (``pt_*``) is a port of Fincept's ``PaperTrading.cpp``:
multi-portfolio, per-product leverage, margin blocking, market/limit/stop
orders with a rejection contract, a netting fill engine (closing legs release
held margin + realize P&L; opening legs convert blocked margin into position
margin), intraday auto square-off and product conversion.
"""

from __future__ import annotations

import json
import logging
import math
import secrets
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import settings
from .db import Database

logger = logging.getLogger(__name__)

_HORIZONS = (5, 15, 30, 60)
#: NEUTRAL verdicts count as correct when the realized move stays within this band.
_NEUTRAL_BAND = 0.005


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_decision_id() -> str:
    return f"DEC-{int(time.time() * 1000)}-{secrets.token_hex(2).upper()}"


def _new_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Order-list helpers (pure functions over a hypothetical order list)
#
# Used by simulation.py / replay to compute equity and positions along a bar
# series without touching the live pt_* engine. Kept here so the backtest
# replay stays isolated from the paper portfolio (it never reads/writes pt_*
# tables). Ported verbatim from the legacy paper module.
# ---------------------------------------------------------------------------


def positions_from_orders(orders: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Derive LONG/SHORT positions (weighted-average cost basis) + realized P&L.

    Raises on an impossible sequence (e.g. SELL more than held) so invalid
    simulated orders are never silently created.
    """
    pos: dict[tuple[str, str], dict[str, Any]] = {}
    for o in orders:
        key = (o["market"], o["ticker"].upper())
        p = pos.setdefault(key, {"direction": None, "qty": 0.0, "entry": 0.0, "realized": 0.0})
        side = o["side"]
        if side == "CLOSE":
            continue
        q, pr, fee = float(o["quantity"]), float(o["price"]), float(o["fee"])
        if side == "BUY":
            if p["direction"] == "SHORT":
                raise ValueError(f"cannot BUY {key} while SHORT — cover first")
            p["direction"] = "LONG"
            cost = p["entry"] * p["qty"] + pr * q
            p["qty"] += q
            p["entry"] = cost / p["qty"] if p["qty"] else 0.0
        elif side == "SELL":
            if p["direction"] != "LONG" or p["qty"] < q - 1e-9:
                raise ValueError(f"cannot SELL {q} of {key} — long {p['qty']}")
            p["realized"] += (pr - p["entry"]) * q - fee
            p["qty"] -= q
            if p["qty"] <= 1e-9:
                p.update(direction=None, qty=0.0, entry=0.0)
        elif side == "SHORT":
            if p["direction"] == "LONG":
                raise ValueError(f"cannot SHORT {key} while LONG — close long first")
            p["direction"] = "SHORT"
            cost = p["entry"] * p["qty"] + pr * q
            p["qty"] += q
            p["entry"] = cost / p["qty"] if p["qty"] else 0.0
        elif side == "COVER":
            if p["direction"] != "SHORT" or p["qty"] < q - 1e-9:
                raise ValueError(f"cannot COVER {q} of {key} — short {p['qty']}")
            p["realized"] += (p["entry"] - pr) * q - fee
            p["qty"] -= q
            if p["qty"] <= 1e-9:
                p.update(direction=None, qty=0.0, entry=0.0)
    for p in pos.values():
        p["qty"] = round(p["qty"], 6)
    return pos


def _cash_from_orders(starting: float, orders: list[dict[str, Any]]) -> float:
    cash = starting
    for o in orders:
        q, pr, fee = float(o["quantity"]), float(o["price"]), float(o["fee"])
        if o["side"] == "BUY":
            cash -= q * pr + fee
        elif o["side"] in ("SELL", "CLOSE"):
            cash += q * pr - fee
        elif o["side"] == "SHORT":
            cash += q * pr - fee
        elif o["side"] == "COVER":
            cash -= q * pr + fee
    return cash


def _net_market_value(
    positions: dict[tuple[str, str], dict[str, Any]],
    prices: dict[tuple[str, str], float],
) -> float:
    """Long market value minus short market value (equity contribution)."""
    net = 0.0
    for key, p in positions.items():
        if p["qty"] <= 0:
            continue
        cur = prices.get(key) or p["entry"]
        if p["direction"] == "LONG":
            net += cur * p["qty"]
        elif p["direction"] == "SHORT":
            net -= cur * p["qty"]
    return net


def realized_per_order(orders: list[dict[str, Any]]) -> dict[str, float]:
    """Realized P&L attributed to each closing order (SELL/COVER)."""
    pos: dict[tuple[str, str], dict[str, Any]] = {}
    out: dict[str, float] = {}
    for o in orders:
        key = (o["market"], o["ticker"].upper())
        p = pos.setdefault(key, {"direction": None, "qty": 0.0, "entry": 0.0})
        side, q, pr, fee = o["side"], float(o["quantity"]), float(o["price"]), float(o["fee"])
        if side == "BUY":
            p["direction"] = "LONG"
            p["entry"] = (p["entry"] * p["qty"] + pr * q) / (p["qty"] + q)
            p["qty"] += q
        elif side == "SHORT":
            p["direction"] = "SHORT"
            p["entry"] = (p["entry"] * p["qty"] + pr * q) / (p["qty"] + q)
            p["qty"] += q
        elif side == "SELL":
            out[o["order_id"]] = (pr - p["entry"]) * q - fee
            p["qty"] -= q
            if p["qty"] <= 1e-9:
                p.update(direction=None, qty=0.0, entry=0.0)
        elif side == "COVER":
            out[o["order_id"]] = (p["entry"] - pr) * q - fee
            p["qty"] -= q
            if p["qty"] <= 1e-9:
                p.update(direction=None, qty=0.0, entry=0.0)
    return out


# ---------------------------------------------------------------------------
# Decision snapshots (immutable, append-only)
# ---------------------------------------------------------------------------


def record_decision_snapshot(
    db: Database, market: str, ticker: str, vdict: dict[str, Any]
) -> str | None:
    """Persist an immutable snapshot of a completed CommitteeDecision.

    Called from the single analysis path (live_verdict). Returns the new
    decision_id or None if a snapshot for this exact timestamp already exists.
    """
    from .dossier import committee_decision

    decision = committee_decision(vdict)
    verdict = decision.get("verdict")
    if verdict in (None, "N/A"):
        return None
    decided_at = decision.get("decision_timestamp") or _now_iso()
    price = (vdict.get("price") or {})
    reference_price = price.get("close")
    # Freshness metadata travels inside the immutable snapshot (no schema change)
    # so analytics can later separate READY from STALE decisions honestly.
    decision["freshness"] = {
        "price_status": price.get("data_status") or "ready",
        "as_of": price.get("as_of") or "",
        "news_available": bool(vdict.get("news_available")),
    }
    decision_id = _new_decision_id()
    inserted = db.insert_decision_snapshot(
        decision_id=decision_id,
        market=market,
        ticker=ticker,
        decided_at=decided_at,
        verdict=verdict,
        conviction=decision.get("conviction"),
        reference_price=reference_price,
        research_confidence=decision.get("research_confidence"),
        decision_json=json.dumps(decision),
    )
    return decision_id if inserted else None


# ---------------------------------------------------------------------------
# Historical evaluation (post-decision prices only, no look-ahead)
# ---------------------------------------------------------------------------


def _bar_time(bar: dict[str, Any]) -> datetime | None:
    try:
        ts = datetime.fromisoformat(bar.get("date", ""))
    except (ValueError, TypeError):
        return None
    # Historical bars are stored naive (UTC); decision timestamps are aware.
    # Normalize so comparisons never raise and silently skip evaluations.
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


def _symbol_for(db: Database, market: str, ticker: str) -> str:
    sec = db.securities_map().get((market, ticker.upper()))
    if sec and sec.get("symbol"):
        return sec["symbol"]
    from .markets import load_markets

    m = load_markets(settings.markets_dir).get(market)
    return f"{ticker}{m.yahoo_suffix if m else ''}"


def evaluate_snapshot(snapshot: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Measure a decision against the FIRST valid price after its timestamp.

    Only bars strictly after ``decided_at`` are used (no look-ahead). Forward
    prices at 5/15/30/60 minutes and end-of-session close. If no reference bar
    is available, the evaluation is NO_DATA.
    """
    try:
        decision_time = datetime.fromisoformat(snapshot["decided_at"])
    except (ValueError, TypeError):
        decision_time = None
    if decision_time is None:
        return {"status": "no_data", "reference_price": None, "correct": None}
    if decision_time.tzinfo is None:
        decision_time = decision_time.replace(tzinfo=UTC)  # naive stored timestamps are UTC

    times = [_bar_time(b) for b in bars]
    ref_idx = next(
        (i for i, t in enumerate(times) if t is not None and t > decision_time), None
    )
    if ref_idx is None:
        return {"status": "no_data", "reference_price": None, "correct": None}

    reference = float(bars[ref_idx]["close"])
    prices: dict[str, float | None] = {"p5": None, "p15": None, "p30": None, "p60": None, "close": None}
    for h in _HORIZONS:
        idx = next(
            (
                i
                for i in range(ref_idx, len(times))
                if times[i] is not None and times[i] >= decision_time + timedelta(minutes=h)
            ),
            None,
        )
        if idx is not None:
            prices[f"p{h}"] = float(bars[idx]["close"])
    if bars:
        prices["close"] = float(bars[-1]["close"])

    forward = next((prices[f"p{h}"] for h in (60, 30, 15, 5) if prices[f"p{h}"] is not None), prices["close"])
    correct = None
    verdict = snapshot.get("verdict")
    if forward is not None and reference:
        move = forward / reference - 1.0
        if verdict == "BULL":
            correct = 1 if move > 0 else 0
        elif verdict == "BEAR":
            correct = 1 if move < 0 else 0
        elif verdict == "NEUTRAL":
            # A neutral call is right when price stayed inside the noise band.
            correct = 1 if abs(move) <= _NEUTRAL_BAND else 0

    return {
        "status": "ok",
        "reference_price": round(reference, 6),
        "prices": {k: round(v, 6) if v is not None else None for k, v in prices.items()},
        "move": round(move, 6) if forward is not None and reference else None,
        "correct": correct,
    }


def refresh_evaluations(db: Database, force: bool = False) -> dict[str, Any]:
    """Evaluate recent snapshots once and cache the results. Does not rerun
    evaluation for already-evaluated decisions unless forced."""
    from .indexes import index_history

    evaluated = db.decision_evaluations()
    done = no_data = 0
    for snapshot in db.decision_snapshots(limit=200):
        if snapshot["decision_id"] in evaluated and not force:
            continue
        try:
            symbol = _symbol_for(db, snapshot["market"], snapshot["ticker"])
            bars = index_history(symbol, "1d")
            result = evaluate_snapshot(snapshot, bars)
        except Exception as exc:
            logger.warning("Evaluation failed for %s: %s", snapshot["decision_id"], exc)
            continue
        if result["status"] == "ok":
            db.insert_decision_evaluation(
                snapshot["decision_id"],
                result["reference_price"],
                result["prices"],
                result["correct"],
                "ok",
            )
            done += 1
        else:
            no_data += 1
    return {"evaluated": done, "no_data": no_data}


# ===========================================================================
# Paper trading engine v2 — Fincept port (simulation only)
# ===========================================================================
#
# Ported from fincept-qt/src/trading/PaperTrading.cpp + TradingTypes.h.
# Adapted to this multi-market equity terminal:
#   * Positions/orders are keyed by (market, ticker) — Fincept used a single
#     `symbol`. security_id = "MARKET:TICKER".
#   * Leverage is portfolio-level (`pt_portfolios.leverage`) for equity MIS;
#     CNC/delivery is 1x. Per-product leverage config (futures/options) lives
#     in an in-memory map, like Fincept (no schema column).
#   * Market hours are generalized per exchange (NSE/BSE IST, NYSE ET, LSE GMT,
#     ...); unknown exchanges are 24/7. Fixed UTC offsets (no DST). Enforcement
#     is opt-in per portfolio (default OFF).
#   * Market orders auto-fill at the latest stored price. Limit /
#     stop / stop_limit orders rest pending and are filled by
#     `pt_process_pending_orders` when a fresher price crosses the trigger.
#   * The rejection contract is preserved: an order that fails the margin or
#     market-hours check is persisted with status="rejected" BEFORE raising.

# Per-portfolio leverage config (in-memory; mirrors Fincept's config_map).
_DEFAULT_LEVERAGE = {
    "equity_mis": 5.0,
    "equity_cnc": 1.0,
    "futures": 10.0,
    "options_buy": 1.0,
    "options_sell": 1.0,
}
_leverage_configs: dict[str, dict[str, float]] = {}
_config_lock = threading.Lock()
_fill_lock = threading.Lock()


def pt_set_leverage_config(portfolio_id: str, cfg: dict[str, float]) -> None:
    with _config_lock:
        merged = dict(_DEFAULT_LEVERAGE)
        merged.update(cfg)
        _leverage_configs[portfolio_id] = merged


def pt_get_leverage_config(portfolio_id: str) -> dict[str, float]:
    with _config_lock:
        return dict(_leverage_configs.get(portfolio_id, _DEFAULT_LEVERAGE))


# ---- Instrument-type helpers (suffix-based, Fincept parity) ----


def pt_is_option(symbol: str) -> bool:
    s = str(symbol or "").upper()
    return s.endswith("CE") or s.endswith("PE")


def pt_is_future(symbol: str) -> bool:
    return str(symbol or "").upper().endswith("FUT")


def _product_is_intraday(product: str) -> bool:
    return str(product or "").upper() in ("MIS", "INTRADAY")


# ---- Margin calculation ----


def _select_leverage(
    cfg: dict[str, float], portfolio_leverage: float, ticker: str, product: str, side: str
) -> float:
    if pt_is_future(ticker):
        lev = cfg["futures"]
    elif pt_is_option(ticker):
        lev = cfg["options_buy"] if side.lower() == "buy" else cfg["options_sell"]
    elif _product_is_intraday(product):
        lev = portfolio_leverage if portfolio_leverage > 0 else cfg["equity_mis"]
    else:
        lev = cfg["equity_cnc"]
    if not math.isfinite(lev) or lev <= 0.0:
        lev = 1.0
    return lev


def pt_calculate_required_margin(
    db: Database,
    portfolio_id: str,
    market: str,
    ticker: str,
    product: str,
    quantity: float,
    price: float,
    side: str,
) -> float:
    if not math.isfinite(quantity) or quantity <= 0.0 or not math.isfinite(price) or price <= 0.0:
        return 0.0
    portfolio = db.pt_get_portfolio(portfolio_id)
    portfolio_leverage = float(portfolio["leverage"]) if portfolio else 1.0
    cfg = pt_get_leverage_config(portfolio_id)
    leverage = _select_leverage(cfg, portfolio_leverage, ticker, product, side)
    return (quantity * price) / leverage


# ---- Exchange hours (generalized; fixed UTC offsets, no DST) ----

# exchange -> (utc_offset_hours, open_hhmm, close_hhmm)
_MARKET_HOURS: dict[str, tuple[int, str, str]] = {
    "NSE": (5, "09:15", "15:30"),
    "BSE": (5, "09:15", "15:30"),
    "NFO": (5, "09:15", "15:30"),
    "BFO": (5, "09:15", "15:30"),
    "MCX": (5, "09:00", "23:30"),
    "CDS": (5, "09:00", "17:00"),
    "NYSE": (-5, "09:30", "16:00"),
    "NASDAQ": (-5, "09:30", "16:00"),
    "LSE": (0, "08:00", "16:30"),
    "KRX": (9, "09:00", "15:30"),
    "TSE": (9, "09:00", "15:00"),
    "HKEX": (8, "09:30", "16:00"),
    "ASX": (10, "10:00", "16:00"),
    "XETRA": (1, "09:00", "17:30"),
    "TSX": (-5, "09:30", "16:00"),
    "SGX": (8, "09:00", "17:00"),
}


def _hhmm_to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def pt_is_market_open(exchange: str) -> bool:
    ex = str(exchange or "").upper()
    spec = _MARKET_HOURS.get(ex)
    if spec is None:
        return True  # unknown / crypto / international: 24/7
    offset, open_hh, close_hh = spec
    now_utc_min = datetime.now(UTC).hour * 60 + datetime.now(UTC).minute
    local_min = (now_utc_min + offset * 60) % (24 * 60)
    return _hhmm_to_minutes(open_hh) <= local_min <= _hhmm_to_minutes(close_hh)


# ---- Portfolio operations ----


def pt_create_portfolio(
    db: Database,
    name: str,
    balance: float,
    user_id: str = "",
    currency: str = "USD",
    leverage: float = 1.0,
    margin_mode: str = "cross",
    fee_rate: float = 0.001,
    exchange: str = "",
) -> dict[str, Any]:
    if not name:
        raise ValueError("Portfolio name cannot be empty")
    if not math.isfinite(balance) or balance <= 0.0:
        raise ValueError("Invalid balance: must be positive")
    if not math.isfinite(leverage) or leverage <= 0.0:
        raise ValueError("Invalid leverage: must be positive")
    if not math.isfinite(fee_rate) or fee_rate < 0.0 or fee_rate > 1.0:
        raise ValueError("Invalid fee_rate: must be between 0 and 1")
    portfolio = {
        "id": _new_id(),
        "name": name,
        "user_id": user_id,
        "initial_balance": balance,
        "balance": balance,
        "currency": currency,
        "leverage": leverage,
        "margin_mode": margin_mode,
        "fee_rate": fee_rate,
        "exchange": exchange,
        "enforce_market_hours": False,
        "created_at": _now_iso(),
    }
    db.pt_insert_portfolio(portfolio)
    return portfolio


def pt_get_portfolio(db: Database, portfolio_id: str) -> dict[str, Any]:
    p = db.pt_get_portfolio(portfolio_id)
    if p is None:
        raise ValueError("Portfolio not found")
    return p


def pt_list_portfolios(db: Database, user_id: str = "") -> list[dict[str, Any]]:
    return db.pt_list_portfolios(user_id)


def pt_find_portfolio(db: Database, name: str, user_id: str = "") -> dict[str, Any] | None:
    return db.pt_find_portfolio(name, user_id)


def pt_set_balance(db: Database, portfolio_id: str, new_balance: float) -> None:
    if not math.isfinite(new_balance) or new_balance < 0.0:
        raise ValueError("Invalid balance: must be finite and non-negative")
    db.pt_update_balance(portfolio_id, new_balance)


def pt_set_enforce_market_hours(db: Database, portfolio_id: str, enforce: bool) -> None:
    db.pt_set_enforce_market_hours(portfolio_id, enforce)


def pt_delete_portfolio(db: Database, portfolio_id: str) -> None:
    pt_get_portfolio(db, portfolio_id)
    db.pt_delete_portfolio(portfolio_id)


def pt_reset_portfolio(db: Database, portfolio_id: str) -> dict[str, Any]:
    portfolio = pt_get_portfolio(db, portfolio_id)
    with _fill_lock:
        with db.transaction() as conn:
            for table in ("pt_trades", "pt_margin_blocks", "pt_positions", "pt_orders"):
                conn.execute(f"DELETE FROM {table} WHERE portfolio_id = ?", (portfolio_id,))
            conn.execute("DELETE FROM paper_equity_points WHERE session_id = ?", (portfolio_id,))
            conn.execute(
                "UPDATE pt_portfolios SET balance = initial_balance WHERE id = ?",
                (portfolio_id,),
            )
        with _config_lock:
            _leverage_configs.pop(portfolio_id, None)
    return pt_get_portfolio(db, portfolio_id)


def ensure_default_portfolio(db: Database, user_id: str = "") -> dict[str, Any]:
    """Find or auto-create the user's default 'Main' portfolio (backward-compat
    with the legacy single-session UX)."""
    existing = db.pt_find_portfolio("Main", user_id)
    if existing:
        return existing
    return pt_create_portfolio(
        db,
        name="Main",
        balance=settings.paper_starting_cash,
        user_id=user_id,
        leverage=1.0,
        fee_rate=0.001,
    )


# ---- Price helpers ----


def _execution_price(db: Database, symbol: str, market: str, ticker: str) -> float | None:
    """Latest available market price (intraday bar first, then stored snapshot)."""
    try:
        from .indexes import index_history

        bars = index_history(symbol, "1d")
        if bars:
            return float(bars[-1]["close"])
    except Exception:
        pass
    snap = None
    try:
        snap = db.latest_price_snapshot(market, ticker)
    except Exception:
        snap = None
    if snap and snap.get("close"):
        return float(snap["close"])
    return None


def pt_quote(db: Database, market: str, ticker: str) -> dict[str, Any]:
    """Lightweight simulated quote (latest intraday bar, else stored snapshot)."""
    symbol = _symbol_for(db, market, ticker)
    price = _execution_price(db, symbol, market, ticker)
    if price is None:
        return {
            "market": market, "ticker": ticker.upper(), "symbol": symbol,
            "security_id": f"{market}:{ticker.upper()}", "price": None, "status": "no_data",
        }
    return {
        "market": market, "ticker": ticker.upper(), "symbol": symbol,
        "security_id": f"{market}:{ticker.upper()}", "price": round(price, 6), "status": "ok",
    }


# ---- Order placement / rejection contract ----


def _reject_order(
    db: Database,
    portfolio_id: str,
    user_id: str,
    market: str,
    ticker: str,
    side: str,
    order_type: str,
    quantity: float,
    price: float | None,
    stop_price: float | None,
    reduce_only: bool,
    product: str,
    exchange: str,
    decision_id: str | None,
    reason_text: str,
) -> None:
    rej = {
        "id": _new_id(), "portfolio_id": portfolio_id, "user_id": user_id,
        "security_id": f"{market}:{ticker.upper()}", "market": market, "ticker": ticker.upper(),
        "side": side, "order_type": order_type, "quantity": quantity, "price": price,
        "stop_price": stop_price, "filled_qty": 0.0, "avg_price": None, "status": "rejected",
        "reduce_only": reduce_only, "margin_blocked": 0.0, "product": product, "exchange": exchange,
        "decision_id": decision_id, "reason": reason_text, "created_at": _now_iso(), "filled_at": None,
    }
    try:
        db.pt_insert_order(rej)
    except Exception:
        pass
    raise ValueError(reason_text)


def pt_place_order(
    db: Database,
    portfolio_id: str,
    market: str,
    ticker: str,
    side: str,
    order_type: str,
    quantity: float,
    price: float | None = None,
    stop_price: float | None = None,
    reduce_only: bool = False,
    product: str = "",
    exchange: str = "",
    decision_id: str | None = None,
    reason: str = "",
    user_id: str = "",
) -> dict[str, Any]:
    side = side.lower()
    order_type = order_type.lower()
    if order_type == "stop_loss":
        order_type = "stop"
    elif order_type == "stop_loss_limit":
        order_type = "stop_limit"

    if order_type not in ("market", "limit", "stop", "stop_limit"):
        raise ValueError(f"Invalid order type: {order_type}")
    if side not in ("buy", "sell"):
        raise ValueError(f"Invalid side: {side}")
    if not math.isfinite(quantity) or quantity <= 0.0:
        raise ValueError("Invalid quantity")
    if price is not None and (not math.isfinite(price) or price <= 0.0):
        raise ValueError("Invalid price")
    if stop_price is not None and (not math.isfinite(stop_price) or stop_price <= 0.0):
        raise ValueError("Invalid stop price")
    if order_type == "limit" and price is None:
        raise ValueError("Limit order requires price")
    if order_type in ("stop", "stop_limit") and stop_price is None:
        raise ValueError("Stop order requires stop_price")

    with _fill_lock:
        portfolio = pt_get_portfolio(db, portfolio_id)

        if reduce_only:
            opposite_side = "short" if side == "buy" else "long"
            opposite = db.pt_find_position(portfolio_id, market, ticker, opposite_side)
            available = float(opposite["quantity"]) if opposite else 0.0
            committed = sum(
                max(0.0, float(item["quantity"]) - float(item["filled_qty"]))
                for status in ("pending", "partial")
                for item in db.pt_get_orders(portfolio_id, status)
                if bool(item["reduce_only"])
                and item["side"] == side
                and item["market"] == market
                and item["ticker"] == ticker.upper()
            )
            if quantity > max(0.0, available - committed) + 1e-9:
                _reject_order(
                    db, portfolio_id, user_id, market, ticker, side, order_type, quantity,
                    price, stop_price, reduce_only, product, exchange, decision_id,
                    "Insufficient shares for reduce-only order",
                )

        if portfolio["enforce_market_hours"] and exchange and not pt_is_market_open(exchange):
            _reject_order(
                db, portfolio_id, user_id, market, ticker, side, order_type, quantity,
                price, stop_price, reduce_only, product, exchange, decision_id,
                f"Market closed for exchange {exchange}",
            )

        # Market orders fill immediately at the latest price. Resolve
        # that price ONCE here so margin calculation and the fill use the same
        # reference (a second fetch could return a stale/different quote).
        market_ref = None
        if order_type == "market":
            market_ref = _execution_price(db, _symbol_for(db, market, ticker), market, ticker)
            if market_ref is None or market_ref <= 0.0:
                _reject_order(
                    db, portfolio_id, user_id, market, ticker, side, order_type, quantity,
                    price, stop_price, reduce_only, product, exchange, decision_id,
                    "No valid execution price for market order",
                )

        # Margin blocking — skip for reduce_only; only the net-new exposure (the
        # portion that is NOT closing an opposite position) consumes margin.
        margin_to_block = 0.0
        if not reduce_only:
            opposite_side = "short" if side == "buy" else "long"
            opposite = db.pt_find_position(portfolio_id, market, ticker, opposite_side)
            net_new = quantity
            if opposite:
                net_new = max(0.0, quantity - float(opposite["quantity"]))
            if net_new > 0.0:
                ref = price if price is not None else (stop_price if stop_price is not None else 0.0)
                if ref <= 0.0:
                    # Market order: use the single resolved reference price.
                    ref = market_ref or 0.0
                if ref <= 0.0:
                    _reject_order(
                        db, portfolio_id, user_id, market, ticker, side, order_type, quantity,
                        price, stop_price, reduce_only, product, exchange, decision_id,
                        "No reference price for margin calculation",
                    )
                margin_to_block = pt_calculate_required_margin(
                    db, portfolio_id, market, ticker, product, net_new, ref, side
                )
                estimated_fee = quantity * ref * float(portfolio["fee_rate"])
                if margin_to_block + estimated_fee > float(portfolio["balance"]) + 1e-6:
                    _reject_order(
                        db, portfolio_id, user_id, market, ticker, side, order_type, quantity,
                        price, stop_price, reduce_only, product, exchange, decision_id,
                        "Insufficient buying power",
                    )

        order = {
            "id": _new_id(), "portfolio_id": portfolio_id, "user_id": user_id,
            "security_id": f"{market}:{ticker.upper()}", "market": market, "ticker": ticker.upper(),
            "side": side, "order_type": order_type, "quantity": quantity, "price": price,
            "stop_price": stop_price, "filled_qty": 0.0, "avg_price": None, "status": "pending",
            "reduce_only": reduce_only, "margin_blocked": margin_to_block, "product": product,
            "exchange": exchange, "decision_id": decision_id, "reason": reason,
            "created_at": _now_iso(), "filled_at": None,
        }
        db.pt_insert_order(order)

        if margin_to_block > 0.0:
            db.pt_update_balance(portfolio_id, float(portfolio["balance"]) - margin_to_block)
            db.pt_insert_margin_block(
                _new_id(), portfolio_id, order["id"], order["security_id"], margin_to_block
            )

        # Market orders fill immediately at the latest resolved market price.
        if order_type == "market":
            _fill_order_impl(db, order["id"], market_ref, None, _now_iso())

        return db.pt_get_order(order["id"])


def pt_cancel_order(db: Database, order_id: str) -> None:
    with _fill_lock:
        order = db.pt_get_order(order_id)
        if order is None:
            raise ValueError("Order not found")
        if order["status"] not in ("pending", "partial"):
            raise ValueError(f"Order is not cancellable ({order['status']})")
        blocked = db.pt_get_margin_block(order_id)
        if blocked > 0.0:
            portfolio = db.pt_get_portfolio(order["portfolio_id"])
            if portfolio:
                db.pt_update_balance(portfolio["id"], float(portfolio["balance"]) + blocked)
            db.pt_delete_margin_block(order_id)
        db.pt_cancel_order(order_id)


# ---- Fill engine (core) ----


def pt_fill_order(
    db: Database,
    order_id: str,
    fill_price: float,
    fill_qty: float | None = None,
    fill_time: str = "",
) -> dict[str, Any]:
    if not math.isfinite(fill_price) or fill_price <= 0.0:
        raise ValueError("Invalid fill price")
    if fill_qty is not None and (not math.isfinite(fill_qty) or fill_qty <= 0.0):
        raise ValueError("Invalid fill quantity")
    with _fill_lock:
        return _fill_order_impl(db, order_id, fill_price, fill_qty, fill_time)


def _fill_order_impl(
    db: Database,
    order_id: str,
    fill_price: float,
    fill_qty: float | None,
    fill_time: str,
) -> dict[str, Any]:
    order = db.pt_get_order(order_id)
    if order is None:
        raise ValueError("Order not found")
    if order["status"] not in ("pending", "partial"):
        raise ValueError("Order not fillable")

    portfolio = db.pt_get_portfolio(order["portfolio_id"])
    remaining_qty = float(order["quantity"]) - float(order["filled_qty"])
    qty = fill_qty if fill_qty is not None else remaining_qty
    if qty <= 0.0:
        raise ValueError("Nothing left to fill")
    if qty > remaining_qty + 1e-9:
        raise ValueError("Fill quantity exceeds remaining order quantity")
    fee_rate = float(portfolio["fee_rate"])
    fee = qty * fill_price * fee_rate
    now = fill_time or _now_iso()

    position_side = "long" if order["side"] == "buy" else "short"
    opposite_side = "short" if order["side"] == "buy" else "long"
    pnl = 0.0
    new_filled = float(order["filled_qty"]) + qty
    fully_filled = new_filled >= float(order["quantity"]) - 1e-9

    with db.transaction() as conn:
        balance_delta = -fee
        order_blocked = 0.0
        row = conn.execute(
            "SELECT amount FROM pt_margin_blocks WHERE order_id = ? LIMIT 1", (order_id,)
        ).fetchone()
        if row:
            order_blocked = float(row["amount"])
        block_consumed = 0.0

        # Closing leg: net against an opposite-side position.
        close_qty = 0.0
        opp = conn.execute(
            "SELECT * FROM pt_positions WHERE portfolio_id = ? AND market = ? AND ticker = ? AND side = ? LIMIT 1",
            (order["portfolio_id"], order["market"], order["ticker"], opposite_side),
        ).fetchone()
        if opp:
            pos = dict(opp)
            close_qty = min(qty, float(pos["quantity"]))
            if pos["side"] == "long":
                pnl = (fill_price - float(pos["entry_price"])) * close_qty
            else:
                pnl = (float(pos["entry_price"]) - fill_price) * close_qty
            frac_close = (close_qty / float(pos["quantity"])) if float(pos["quantity"]) > 0 else 1.0
            margin_released = float(pos["held_margin"]) * frac_close
            if close_qty >= float(pos["quantity"]) - 1e-9:
                conn.execute("DELETE FROM pt_positions WHERE id = ?", (pos["id"],))
            else:
                conn.execute(
                    "UPDATE pt_positions SET quantity = ?, entry_price = ? WHERE id = ?",
                    (float(pos["quantity"]) - close_qty, float(pos["entry_price"]), pos["id"]),
                )
                conn.execute(
                    "UPDATE pt_positions SET held_margin = ? WHERE id = ?",
                    (float(pos["held_margin"]) - margin_released, pos["id"]),
                )
                conn.execute(
                    "UPDATE pt_positions SET realized_pnl = realized_pnl + ? WHERE id = ?",
                    (pnl, pos["id"]),
                )
            balance_delta += margin_released + pnl

        # Opening leg: net-new exposure on the order's own side.
        open_qty = qty - close_qty
        if open_qty > 0.0:
            open_margin = pt_calculate_required_margin(
                db, order["portfolio_id"], order["market"], order["ticker"],
                order["product"], open_qty, fill_price, order["side"],
            )
            block_consumed = order_blocked if fully_filled else min(order_blocked, open_margin)
            balance_delta += block_consumed - open_margin

            product = order["product"] or "MIS"
            same = conn.execute(
                "SELECT * FROM pt_positions WHERE portfolio_id = ? AND market = ? AND ticker = ? AND side = ? LIMIT 1",
                (order["portfolio_id"], order["market"], order["ticker"], position_side),
            ).fetchone()
            if same:
                pos = dict(same)
                new_qty = float(pos["quantity"]) + open_qty
                if new_qty <= 0.0:
                    raise ValueError("Invalid position quantity after averaging")
                new_entry = (float(pos["entry_price"]) * float(pos["quantity"]) + fill_price * open_qty) / new_qty
                conn.execute(
                    "UPDATE pt_positions SET quantity = ?, entry_price = ? WHERE id = ?",
                    (new_qty, new_entry, pos["id"]),
                )
                conn.execute(
                    "UPDATE pt_positions SET held_margin = ? WHERE id = ?",
                    (float(pos["held_margin"]) + open_margin, pos["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO pt_positions
                       (id, portfolio_id, user_id, security_id, market, ticker, side,
                        quantity, entry_price, current_price, unrealized_pnl, realized_pnl,
                        leverage, product, held_margin, opened_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        _new_id(), order["portfolio_id"], order.get("user_id", ""),
                        order.get("security_id", ""), order["market"], order["ticker"], position_side,
                        open_qty, fill_price, fill_price, 0.0, 0.0,
                        (open_qty * fill_price) / open_margin if open_margin > 0 else float(portfolio["leverage"]),
                        product, open_margin, now,
                    ),
                )

        # Settle the order's margin block.
        if order_blocked > 0.0:
            if fully_filled:
                conn.execute("DELETE FROM pt_margin_blocks WHERE order_id = ?", (order_id,))
            else:
                remaining = max(0.0, order_blocked - block_consumed)
                conn.execute("DELETE FROM pt_margin_blocks WHERE order_id = ?", (order_id,))
                conn.execute(
                    "INSERT INTO pt_margin_blocks (id, portfolio_id, order_id, security_id, amount) VALUES (?, ?, ?, ?, ?)",
                    (_new_id(), order["portfolio_id"], order_id, order.get("security_id", ""), remaining),
                )

        resulting_balance = float(portfolio["balance"]) + balance_delta
        if resulting_balance < -1e-6:
            raise ValueError("Insufficient buying power at execution price")

        conn.execute(
            "UPDATE pt_portfolios SET balance = balance + ? WHERE id = ?",
            (balance_delta, order["portfolio_id"]),
        )

        new_status = "filled" if fully_filled else "partial"
        prev_avg = float(order["avg_price"]) if order["avg_price"] is not None else 0.0
        new_avg = (prev_avg * float(order["filled_qty"]) + fill_price * qty) / new_filled if new_filled > 0 else fill_price
        conn.execute(
            "UPDATE pt_orders SET filled_qty = ?, avg_price = ?, status = ?, filled_at = ? WHERE id = ?",
            (new_filled, new_avg, new_status, now, order_id),
        )

        trade = {
            "id": _new_id(), "portfolio_id": order["portfolio_id"], "order_id": order_id,
            "security_id": order.get("security_id", ""), "market": order["market"],
            "ticker": order["ticker"], "side": order["side"], "price": fill_price,
            "quantity": qty, "fee": fee, "pnl": pnl - fee, "timestamp": now,
        }
        conn.execute(
            """INSERT INTO pt_trades
               (id, portfolio_id, order_id, security_id, market, ticker, side,
                price, quantity, fee, pnl, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade["id"], trade["portfolio_id"], trade["order_id"], trade["security_id"],
                trade["market"], trade["ticker"], trade["side"], trade["price"], trade["quantity"],
                trade["fee"], trade["pnl"], trade["timestamp"],
            ),
        )
        return trade


# ---- Position / trade / order queries ----


def pt_get_positions(db: Database, portfolio_id: str) -> list[dict[str, Any]]:
    positions = db.pt_get_positions(portfolio_id)
    # Refresh current prices + unrealized P&L from the latest snapshots.
    for p in positions:
        sym = _symbol_for(db, p["market"], p["ticker"])
        price = _execution_price(db, sym, p["market"], p["ticker"])
        if price is not None and price > 0:
            db.pt_update_position_price(portfolio_id, p["market"], p["ticker"], price)
            p["current_price"] = price
        cur = float(p.get("current_price") or p["entry_price"])
        if p["side"] == "long":
            p["unrealized_pnl"] = (cur - float(p["entry_price"])) * float(p["quantity"])
        else:
            p["unrealized_pnl"] = (float(p["entry_price"]) - cur) * float(p["quantity"])
    return positions


def pt_update_position_price(db: Database, portfolio_id: str, market: str, ticker: str, price: float) -> None:
    db.pt_update_position_price(portfolio_id, market, ticker, price)


def pt_get_orders(db: Database, portfolio_id: str, status: str = "") -> list[dict[str, Any]]:
    return db.pt_get_orders(portfolio_id, status)


def pt_get_trades(db: Database, portfolio_id: str, limit: int = 100) -> list[dict[str, Any]]:
    return db.pt_get_trades(portfolio_id, limit)


def pt_get_orders_for_day(db: Database, portfolio_id: str, day_iso: str) -> list[dict[str, Any]]:
    start = f"{day_iso}T00:00:00+00:00"
    end = f"{day_iso}T23:59:59.999999+00:00"
    return db.pt_get_orders_between(portfolio_id, start, end)


def pt_get_trades_for_day(db: Database, portfolio_id: str, day_iso: str) -> list[dict[str, Any]]:
    start = f"{day_iso}T00:00:00+00:00"
    end = f"{day_iso}T23:59:59.999999+00:00"
    return db.pt_get_trades_between(portfolio_id, start, end)


# ---- Stats ----


def pt_get_stats(db: Database, portfolio_id: str) -> dict[str, Any]:
    trades = db.pt_get_trades(portfolio_id, limit=100000)
    realized = [float(t["pnl"]) for t in trades]
    wins = [v for v in realized if v > 1e-9]
    losses = [v for v in realized if v < -1e-9]
    n = len(realized)
    gross_profit = sum(wins)
    gross_loss = sum(losses)  # <= 0
    total_fees = sum(float(t["fee"]) for t in trades)
    turnover = sum(float(t["price"]) * float(t["quantity"]) for t in trades)
    today = datetime.now(UTC).date().isoformat()
    today_pnl = sum(float(t["pnl"]) for t in trades if str(t["timestamp"]).startswith(today))
    result: dict[str, Any] = {
        "total_pnl": round(sum(realized), 6),
        "total_trades": n,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": round(len(wins) / n, 4) if n else None,
        "largest_win": round(max(wins), 6) if wins else 0.0,
        "largest_loss": round(min(losses), 6) if losses else 0.0,
        "gross_profit": round(gross_profit, 6),
        "gross_loss": round(gross_loss, 6),
        "avg_win": round(gross_profit / len(wins), 6) if wins else 0.0,
        "avg_loss": round(gross_loss / len(losses), 6) if losses else 0.0,
        "profit_factor": round(gross_profit / abs(gross_loss), 6) if gross_loss else (None if not wins else float("inf")),
        "total_fees": round(total_fees, 6),
        "turnover": round(turnover, 6),
        "today_pnl": round(today_pnl, 6),
    }
    return result


# ---- Portfolio state (UI snapshot) ----


def pt_portfolio_state(db: Database, portfolio_id: str, record_equity: bool = True) -> dict[str, Any]:
    portfolio = pt_get_portfolio(db, portfolio_id)
    positions = pt_get_positions(db, portfolio_id)

    long_value = sum(
        float(p["quantity"]) * float(p.get("current_price") or p["entry_price"])
        for p in positions if p["side"] == "long" and float(p["quantity"]) > 0
    )
    short_value = sum(
        float(p["quantity"]) * float(p.get("current_price") or p["entry_price"])
        for p in positions if p["side"] == "short" and float(p["quantity"]) > 0
    )
    gross = long_value + short_value
    net = long_value - short_value

    cash = float(portfolio["balance"])
    unrealized = sum(float(p.get("unrealized_pnl", 0.0)) for p in positions)
    held_margin = sum(float(p.get("held_margin", 0.0)) for p in positions)
    equity = cash + held_margin + unrealized
    initial = float(portfolio["initial_balance"])
    # Realized P&L is the sum of every closed trade's pnl (positions are deleted
    # on full close, so it cannot be read from pt_positions).
    realized_total = sum(float(t["pnl"]) for t in db.pt_get_trades(portfolio_id, limit=100000))
    total_pnl = realized_total + unrealized
    day_pct = (total_pnl / initial * 100) if initial else 0.0

    if record_equity:
        _record_equity_point(db, portfolio_id, equity)

    pos_list = []
    for p in positions:
        if float(p["quantity"]) <= 0:
            continue
        cur = float(p.get("current_price") or p["entry_price"])
        pos_list.append({
            "market": p["market"], "ticker": p["ticker"],
            "security_id": p.get("security_id") or f"{p['market']}:{p['ticker']}",
            "side": p["side"], "quantity": round(float(p["quantity"]), 6),
            "entry_price": round(float(p["entry_price"]), 6),
            "current_price": round(cur, 6),
            "value": round(cur * float(p["quantity"]), 2),
            "unrealized_pnl": round(float(p.get("unrealized_pnl", 0.0)), 2),
            "realized_pnl": round(float(p.get("realized_pnl", 0.0)), 2),
            "leverage": float(p.get("leverage", 1.0)),
            "product": p.get("product", "MIS"),
            "held_margin": round(float(p.get("held_margin", 0.0)), 2),
            "opened_at": p.get("opened_at", ""),
        })

    return {
        "portfolio_id": portfolio["id"],
        "name": portfolio["name"],
        "starting_cash": initial,
        "currency": portfolio.get("currency", "USD"),
        "cash": round(cash, 2),
        "buying_power": round(max(0.0, cash), 2),
        "equity": round(equity, 2),
        "long_value": round(long_value, 2),
        "short_value": round(short_value, 2),
        "gross_exposure": round(gross, 2),
        "net_exposure": round(net, 2),
        "realized_pnl": round(realized_total, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(total_pnl, 2),
        "day_pct": round(day_pct, 4),
        "open_positions": len(pos_list),
        "positions": pos_list,
        "leverage": float(portfolio.get("leverage", 1.0)),
        "enforce_market_hours": bool(portfolio.get("enforce_market_hours", 0)),
    }


def _record_equity_point(db: Database, portfolio_id: str, equity: float) -> None:
    """Record an equity point at most once per 60 seconds per portfolio."""
    last = db.last_equity_at(portfolio_id)
    if last:
        try:
            if (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds() < 60:
                return
        except (ValueError, TypeError):
            pass
    db.insert_equity_point(portfolio_id, round(equity, 2))


def pt_equity_history(db: Database, portfolio_id: str) -> list[dict[str, Any]]:
    return db.equity_points(portfolio_id)


# ---- Risk ----


def pt_risk(db: Database, portfolio_id: str) -> dict[str, Any]:
    state = pt_portfolio_state(db, portfolio_id, record_equity=False)
    positions = state["positions"]
    equity = state["equity"]
    gross = state["gross_exposure"]
    largest = max(positions, key=lambda p: p["value"]) if positions else None
    warnings: list[str] = []
    if equity > 0 and gross > settings.paper_max_gross_ratio * equity:
        warnings.append(f"Gross exposure ({gross:.0f}) exceeds {settings.paper_max_gross_ratio:.1f}x equity")
    if largest and equity > 0:
        pct = largest["value"] / equity
        if pct > settings.paper_max_position_ratio:
            warnings.append(f"{largest['ticker']} is {pct:.0%} of equity (limit {settings.paper_max_position_ratio:.0%})")
    return {
        "gross_exposure": gross,
        "net_exposure": state["net_exposure"],
        "long_exposure": state["long_value"],
        "short_exposure": state["short_value"],
        "largest_position": largest,
        "largest_position_pct": round(largest["value"] / equity, 4) if largest and equity else None,
        "concentration": round(
            max((p["value"] for p in positions), default=0.0) / max(gross, 1.0), 4
        ) if positions else 0.0,
        "warnings": warnings,
    }


# ---- Product conversion ----


def pt_convert_position_product(db: Database, position_id: str, new_product: str) -> None:
    with _fill_lock:
        pos = db.pt_get_position(position_id)
        if pos is None:
            raise ValueError("Position not found")
        if str(pos["product"]).upper() == str(new_product).upper():
            return
        portfolio = db.pt_get_portfolio(pos["portfolio_id"])
        side = "buy" if pos["side"] == "long" else "sell"
        new_margin = pt_calculate_required_margin(
            db, pos["portfolio_id"], pos["market"], pos["ticker"], new_product,
            float(pos["quantity"]), float(pos["entry_price"]), side,
        )
        extra = new_margin - float(pos["held_margin"])
        if extra > float(portfolio["balance"]) + 1e-6:
            raise ValueError(f"Insufficient balance to convert to {new_product}")
        with db.transaction() as conn:
            conn.execute(
                "UPDATE pt_portfolios SET balance = balance - ? WHERE id = ?",
                (extra, pos["portfolio_id"]),
            )
            conn.execute(
                "UPDATE pt_positions SET held_margin = ? WHERE id = ?", (new_margin, position_id)
            )
            conn.execute(
                "UPDATE pt_positions SET product = ? WHERE id = ?", (new_product, position_id)
            )


# ---- Intraday settlement / end of session ----


def _square_off_position(db: Database, pos: dict[str, Any], price: float, stamp_iso: str) -> bool:
    side = "sell" if pos["side"] == "long" else "buy"
    order = {
        "id": _new_id(), "portfolio_id": pos["portfolio_id"], "user_id": pos.get("user_id", ""),
        "security_id": pos.get("security_id", f"{pos['market']}:{pos['ticker']}"),
        "market": pos["market"], "ticker": pos["ticker"], "side": side, "order_type": "market",
        "quantity": float(pos["quantity"]), "price": price, "stop_price": None,
        "filled_qty": 0.0, "avg_price": None, "status": "pending", "reduce_only": True,
        "margin_blocked": 0.0, "product": pos.get("product", "MIS"), "exchange": "",
        "decision_id": None, "reason": "auto square-off", "created_at": stamp_iso, "filled_at": None,
    }
    db.pt_insert_order(order)
    _fill_order_impl(db, order["id"], price, None, stamp_iso)
    return True


def pt_settle_intraday(db: Database, portfolio_id: str) -> int:
    """Square off every intraday (MIS) position at its last known price.

    Auto-squares run only when the portfolio opts in via enforce_market_hours
    and the exchange is past its close; this function is also exposed as a
    manual endpoint. CNC/NRML (delivery) positions are carried forward.
    """
    squared = 0
    positions = db.pt_get_positions(portfolio_id)
    now = _now_iso()
    for pos in positions:
        if not _product_is_intraday(pos.get("product", "")):
            continue
        price = float(pos.get("current_price") or pos["entry_price"])
        if price <= 0.0:
            price = _execution_price(db, _symbol_for(db, pos["market"], pos["ticker"]), pos["market"], pos["ticker"]) or 0.0
        if price <= 0.0:
            continue
        try:
            _square_off_position(db, pos, price, now)
            squared += 1
        except Exception as exc:
            logger.warning("Auto-square failed for %s: %s", pos["ticker"], exc)
    # Cancel stale pending orders from prior sessions.
    for o in db.pt_get_orders(portfolio_id, "pending"):
        try:
            pt_cancel_order(db, o["id"])
        except Exception:
            pass
    return squared


def pt_settle_intraday_all(db: Database) -> int:
    total = 0
    for p in db.pt_list_portfolios():
        total += pt_settle_intraday(db, p["id"])
    return total


def pt_end_session(db: Database, portfolio_id: str) -> dict[str, Any]:
    """Liquidate ALL open positions (intraday + delivery) at the final valid price."""
    closed: list[dict[str, Any]] = []
    positions = db.pt_get_positions(portfolio_id)
    now = _now_iso()
    for pos in positions:
        sym = _symbol_for(db, pos["market"], pos["ticker"])
        price = _execution_price(db, sym, pos["market"], pos["ticker"]) or float(pos["entry_price"])
        if price <= 0:
            continue
        try:
            _square_off_position(db, pos, price, now)
            closed.append({"market": pos["market"], "ticker": pos["ticker"], "price": price})
        except Exception:
            continue
    final = pt_portfolio_state(db, portfolio_id, record_equity=True)
    final["closed_positions"] = closed
    return final


# ---- Pending order matching (limit / stop) ----


def pt_process_pending_orders(db: Database, portfolio_id: str) -> int:
    """Fill any pending limit/stop orders whose trigger condition is met by the
    latest stored price. Called by the background refresh loop. Returns the
    number of orders filled."""
    filled = 0
    pending = db.pt_get_orders(portfolio_id, "pending") + db.pt_get_orders(portfolio_id, "partial")
    for o in pending:
        if o["order_type"] == "market":
            continue
        sym = _symbol_for(db, o["market"], o["ticker"])
        last = _execution_price(db, sym, o["market"], o["ticker"])
        if last is None or last <= 0:
            continue
        stop = float(o["stop_price"]) if o["stop_price"] is not None else None
        limit = float(o["price"]) if o["price"] is not None else None
        side = o["side"]
        otype = o["order_type"]
        trigger = False
        # Fill at the prevailing market price when triggered (at least as good as
        # the limit/stop for the user, since the trigger guarantees last is on the
        # favorable side of the threshold). stop_limit caps at the limit price.
        fill_price = last
        if otype == "limit":
            if side == "buy" and last <= (limit or 0):
                trigger = True
            elif side == "sell" and last >= (limit or float("inf")):
                trigger = True
        elif otype == "stop":
            if side == "buy" and last >= (stop or 0):
                trigger = True
            elif side == "sell" and last <= (stop or float("inf")):
                trigger = True
        elif otype == "stop_limit":
            if side == "buy" and last >= (stop or 0) and last <= (limit or 0):
                trigger = True
            elif side == "sell" and last <= (stop or float("inf")) and last >= (limit or float("inf")):
                trigger = True
        if not trigger:
            continue
        try:
            with _fill_lock:
                _fill_order_impl(db, o["id"], fill_price, None, _now_iso())
            filled += 1
        except Exception as exc:
            if isinstance(exc, ValueError) and "Insufficient buying power" in str(exc):
                blocked = db.pt_get_margin_block(o["id"])
                portfolio = db.pt_get_portfolio(o["portfolio_id"])
                if blocked > 0.0 and portfolio:
                    db.pt_update_balance(o["portfolio_id"], float(portfolio["balance"]) + blocked)
                    db.pt_delete_margin_block(o["id"])
                db.pt_reject_order(o["id"], str(exc))
            logger.warning("Pending fill failed for %s: %s", o["ticker"], exc)
    return filled


# ---- Leaderboard (player + labelled demo accounts) ----


def pt_leaderboard(db: Database, portfolio_id: str) -> dict[str, Any]:
    player = pt_portfolio_state(db, portfolio_id, record_equity=True)
    rows = [
        {
            "rank": 1, "name": "You", "is_demo": False,
            "equity": player["equity"],
            "return": round((player["equity"] - player["starting_cash"]) / player["starting_cash"] * 100, 2)
            if player["starting_cash"] else 0.0,
            "positions": player["open_positions"], "trades": len(db.pt_get_trades(portfolio_id)),
        }
    ]
    if settings.paper_demo_players:
        import hashlib

        seed = int(hashlib.sha256((player["portfolio_id"] + settings.paper_session_end).encode()).hexdigest(), 16)
        for i, name in enumerate(("Demo Alpha", "Demo Beta", "Demo Gamma")):
            r = ((seed >> (i * 11)) % 1000) / 1000.0 * 12.0 - 2.0
            rows.append({
                "rank": i + 2, "name": name, "is_demo": True,
                "equity": round(player["starting_cash"] * (1 + r / 100.0), 2),
                "return": round(r, 2), "positions": None, "trades": None,
            })
    rows.sort(key=lambda r: r["equity"], reverse=True)
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return {"rows": rows, "demo_label": "DEMO COMPETITORS ARE SIMULATED ACCOUNTS — NOT REAL USERS."}


# ===========================================================================
# Performance / research quality (derived from stored snapshots + evaluations)
# ===========================================================================


def _conviction_bucket(conviction: float | None) -> str | None:
    if conviction is None:
        return None
    c = conviction * 100.0
    if c < 50:
        return "0-50"
    if c < 60:
        return "50-60"
    if c < 70:
        return "60-70"
    if c < 80:
        return "70-80"
    return "80-100"


def performance(db: Database) -> dict[str, Any]:
    snapshots = db.decision_snapshots(limit=1000)
    evaluations = db.decision_evaluations()

    bulls = bears = neut = evaluated_count = 0
    correct_count = 0
    correct_total = 0
    bucket_returns: dict[str, list[float]] = {}
    agreement_returns: dict[str, list[float]] = {}
    research_conf: list[float] = []
    coverage_sum = 0
    coverage_n = 0

    for snap in snapshots:
        verdict = snap.get("verdict")
        if verdict == "BULL":
            bulls += 1
        elif verdict == "BEAR":
            bears += 1
        else:
            neut += 1
        ev = evaluations.get(snap["decision_id"])
        if not ev or ev.get("status") != "ok" or not ev.get("reference_price"):
            continue
        evaluated_count += 1
        if ev.get("correct") is not None:
            correct_total += 1
            correct_count += int(ev["correct"])
        forward = ev.get("p30") or ev.get("p15") or ev.get("p60") or ev.get("p5")
        if forward and ev.get("reference_price"):
            ret = forward / ev["reference_price"] - 1.0
            bucket = _conviction_bucket(snap.get("conviction"))
            if bucket:
                bucket_returns.setdefault(bucket, []).append(ret)
            agreement = _agreement_key(snap)
            if agreement:
                agreement_returns.setdefault(agreement, []).append(ret)

        try:
            decision = json.loads(snap.get("decision_json") or "{}")
        except (ValueError, TypeError):
            decision = {}
        if snap.get("research_confidence") is not None:
            research_conf.append(float(snap["research_confidence"]))
        research = decision.get("research") or {}
        provenance = research.get("provenance") or []
        coverage_sum += len(provenance)
        coverage_n += 1

    def _stats(values: list[float]) -> dict[str, Any]:
        if not values:
            return {"avg": None, "n": 0}
        return {"avg": round(sum(values) / len(values), 6), "n": len(values)}

    bucket_table = []
    for label in ("0-50", "50-60", "60-70", "70-80", "80-100"):
        if label in bucket_returns:
            bucket_table.append({"bucket": label, **_stats(bucket_returns[label])})
    agreement_table = [
        {"agreement": k, **_stats(v)}
        for k, v in sorted(agreement_returns.items(), key=lambda kv: len(kv[1]), reverse=True)
    ]

    return {
        "decisions": len(snapshots),
        "bull": bulls,
        "bear": bears,
        "neutral": neut,
        "evaluated": evaluated_count,
        "directional_accuracy": round(correct_count / correct_total, 4) if correct_total else None,
        "conviction_buckets": bucket_table,
        "agreement_returns": agreement_table,
        "research_confidence_avg": round(sum(research_conf) / len(research_conf), 4) if research_conf else None,
        "research_coverage_avg": round(coverage_sum / coverage_n, 2) if coverage_n else None,
        "research_n": coverage_n,
    }


def _agreement_key(snapshot: dict[str, Any]) -> str | None:
    """Signal agreement bucket (e.g. '4/5') from the stored decision signals."""
    try:
        decision = json.loads(snapshot.get("decision_json") or "{}")
    except (ValueError, TypeError):
        return None
    signals = decision.get("signals") or {}
    verdict = snapshot.get("verdict")
    if not signals or verdict not in ("BULL", "BEAR"):
        return None
    aligned = 0
    total = 0
    for s in signals.values():
        if s.get("status") != "AVAILABLE":
            continue
        total += 1
        if s.get("direction") == verdict:
            aligned += 1
    if total == 0:
        return None
    return f"{aligned}/{total}"
