"""Terminal notification service + market event detector.

Detects significant events over the existing data (market sessions, committee
verdict changes, price/volume moves, signal flips, important news, simulated
trades) and records them as notifications with an explicit severity
(INFO / IMPORTANT / HIGH).

Every candidate event has a deterministic key; processed keys are persisted so
repeated polls never duplicate a notification (e.g. a market open fires exactly
once per session).

Events:
* ``market_open``        — a configured market's session has opened (INFO).
* ``committee_change``   — a fresh Committee verdict differs from its previous
                            verdict (IMPORTANT; HIGH on a BULL<->BEAR reversal).
* ``signal_change``      — a non-committee quantitative/technical signal
                            direction flip between verdicts (IMPORTANT).
* ``significant_move``   — close-to-close move at/above the threshold (IMPORTANT).
* ``volume_spike``       — volume at/above the spike multiple of the prior
                            snapshot (IMPORTANT).
* ``important_news``     — a freshly stored article with a strong sentiment
                            score (IMPORTANT).
* ``price_target``       — a user-defined above/below price threshold has been
                            reached (HIGH; the one-shot rule is then disarmed).
* ``significant_trade``  — a paper trade whose notional is at/above the
                            configured threshold (HIGH), or any LONG<->SHORT
                            reversal regardless of size (HIGH).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import settings
from .db import Database

logger = logging.getLogger(__name__)

INFO = "INFO"
IMPORTANT = "IMPORTANT"
HIGH = "HIGH"

#: A committee change is only notified when the latest verdict is this fresh
#: (i.e. it was just recomputed); otherwise old history would flood the feed.
_CHANGE_FRESH_WINDOW = timedelta(hours=24)

#: Freshness window for price/volume events (stored snapshots, not live quotes).
_SNAPSHOT_FRESH_WINDOW = timedelta(hours=24)

#: Freshness window for important-news detection (only freshly STORED articles).
_NEWS_FRESH_WINDOW = timedelta(hours=6)


def _move_threshold() -> float:
    try:
        return float(os.getenv("STOCK_ALERT_MOVE_PCT", "0.03"))
    except (TypeError, ValueError):
        return 0.03


def _volume_multiple() -> float:
    try:
        return float(os.getenv("STOCK_ALERT_VOLUME_MULT", "2.0"))
    except (TypeError, ValueError):
        return 2.0


def _now() -> datetime:
    return datetime.now(UTC)


def _fresh(iso: str | None, window: timedelta, now: datetime) -> bool:
    """True when an ISO timestamp is within ``window`` of ``now``."""
    if not iso:
        return False
    try:
        ts = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return now - ts <= window


def _money(value: float) -> str:
    return f"{value:,.0f}"


def _emit(
    db: Database,
    key: str,
    severity: str,
    type_: str,
    title: str,
    message: str,
    security_id: str = "",
    market: str = "",
    ticker: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Mark the key processed and record the event once. Returns the event."""
    db.mark_notification_processed(key)
    import json

    inserted = db.insert_notification_event(
        key, severity, type_, title, message, security_id, market, ticker,
        json.dumps(payload or {}),
    )
    if not inserted:
        return None
    return {
        "event_key": key,
        "severity": severity,
        "type": type_,
        "title": title,
        "message": message,
        "security_id": security_id,
        "market": market,
        "ticker": ticker,
        "payload": payload or {},
    }


# ---------------------------------------------------------------------------
# Market open detection (once per market session)
# ---------------------------------------------------------------------------


def _market_open_events(db: Database, now: datetime) -> list[dict[str, Any]]:
    from .markets import enabled_market_codes, load_markets, market_status

    enabled = set(enabled_market_codes())
    events: list[dict[str, Any]] = []
    for market in load_markets(settings.markets_dir).values():
        if market.code not in enabled:
            continue
        status = market_status(market, now)
        if status["status"] != "open":
            continue
        key = f"market_open:{market.code}:{status['local_date']}"
        if db.is_notification_processed(key):
            continue
        event = _emit(
            db,
            key,
            INFO,
            "market_open",
            f"MARKET OPEN - {market.code}",
            f"{market.name} opened at {status['opened_at']} local time "
            f"({status['timezone']}). Market is now open.",
            market=market.code,
            payload={"market": market.code, "opened_at": status["opened_at"]},
        )
        if event:
            events.append(event)
    return events


# ---------------------------------------------------------------------------
# Committee change detection
# ---------------------------------------------------------------------------


def _committee_change_events(db: Database, now: datetime) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    cutoff = (now - _CHANGE_FRESH_WINDOW).isoformat()
    pairs = db.verdict_pairs()
    for (market, ticker), (latest, previous) in pairs.items():
        if latest is None or previous is None:
            continue
        if latest["verdict"] == previous["verdict"]:
            continue
        # Only a freshly recomputed verdict is a "change"; old rows are history.
        if latest.get("decided_at", "") < cutoff:
            continue
        prev_v = str(previous["verdict"])
        cur_v = str(latest["verdict"])
        decided_at = str(latest["decided_at"])
        sec = f"{market}:{ticker}"
        reversal = {prev_v, cur_v} == {"BULL", "BEAR"}
        severity = HIGH if reversal else IMPORTANT
        type_ = "committee_reversal" if reversal else "committee_change"
        title = f"{sec} COMMITTEE {prev_v} → {cur_v}"
        message = (
            f"The Committee view for {sec} changed from {prev_v} to {cur_v} "
            f"(conviction {float(latest.get('confidence') or 0.0) * 100:.0f}%)."
        )
        key = f"committee_change:{sec}:{prev_v}:{cur_v}:{decided_at}"
        if db.is_notification_processed(key):
            continue
        event = _emit(
            db, key, severity, type_, title, message, sec, market, ticker,
            payload={"previous_verdict": prev_v, "verdict": cur_v},
        )
        if event:
            events.append(event)
    return events


# ---------------------------------------------------------------------------
# Signal direction change (quantitative/technical, non-committee)
# ---------------------------------------------------------------------------


def _signal_direction(row: dict[str, Any] | None) -> str | None:
    """BULL/BEAR direction of a verdict row's quant signal (NEUTRAL/None = no vote)."""
    if not row:
        return None
    direction = None
    try:
        payload = json.loads(row.get("signals") or "")
        direction = (payload.get("quantitative") or {}).get("direction")
    except (TypeError, ValueError):
        direction = None
    if direction in ("BULL", "BEAR"):
        return direction
    tech = row.get("technical_score")
    try:
        tech = float(tech) if tech is not None else None
    except (TypeError, ValueError):
        tech = None
    if tech is None:
        return None
    if tech > 0.05:
        return "BULL"
    if tech < -0.05:
        return "BEAR"
    return None


def _signal_change_events(db: Database, now: datetime) -> list[dict[str, Any]]:
    """Quant/technical signal direction flip (independent of committee verdict)."""
    events: list[dict[str, Any]] = []
    cutoff = (now - _CHANGE_FRESH_WINDOW).isoformat()
    for (market, ticker), (latest, previous) in db.verdict_pairs().items():
        if latest is None or previous is None:
            continue
        if latest.get("decided_at", "") < cutoff:
            continue  # only freshly recomputed signals are "changes"
        prev_d = _signal_direction(previous)
        cur_d = _signal_direction(latest)
        if not prev_d or not cur_d or prev_d == cur_d:
            continue
        sec = f"{market}:{ticker}"
        decided_at = str(latest.get("decided_at"))
        key = f"signal_change:{sec}:{prev_d}:{cur_d}:{decided_at}"
        if db.is_notification_processed(key):
            continue
        event = _emit(
            db, key, IMPORTANT, "signal_change",
            f"{sec} SIGNAL {prev_d} → {cur_d}",
            f"The quantitative/technical signal for {sec} flipped from {prev_d} to {cur_d}.",
            sec, market, ticker,
            payload={"previous": prev_d, "signal": cur_d},
        )
        if event:
            events.append(event)
    return events


# ---------------------------------------------------------------------------
# Significant price move + volume spike (stored snapshot pairs)
# ---------------------------------------------------------------------------


def _market_activity_events(db: Database, now: datetime) -> list[dict[str, Any]]:
    """Close-to-close move and volume-spike detection over stored snapshots."""
    events: list[dict[str, Any]] = []
    move_th = _move_threshold()
    vol_mult = _volume_multiple()
    for (market, ticker), (latest, previous) in db.price_snapshot_pairs().items():
        if not latest or not previous:
            continue
        # Only fresh snapshots (a real recent refresh) may fire, so a first scan
        # over historical data never floods the feed.
        if not _fresh(latest.get("fetched_at"), _SNAPSHOT_FRESH_WINDOW, now):
            continue
        sec = f"{market}:{ticker}"
        day = str(latest.get("fetched_at"))[:10]
        try:
            close = float(latest.get("close") or 0.0)
            prev_close = float(previous.get("close") or 0.0)
            cur_vol = float(latest.get("volume") or 0.0)
            prev_vol = float(previous.get("volume") or 0.0)
        except (TypeError, ValueError):
            continue
        if close <= 0 or prev_close <= 0:
            continue
        move = close / prev_close - 1.0
        if abs(move) >= move_th:
            direction = "up" if move > 0 else "down"
            key = f"price_move:{sec}:{direction}:{day}"
            if not db.is_notification_processed(key):
                severity = HIGH if abs(move) >= move_th * 2 else IMPORTANT
                event = _emit(
                    db, key, severity, "significant_move",
                    f"{sec} {'+' if move > 0 else ''}{move * 100:.1f}% MOVE",
                    f"{sec} moved {move * 100:+.1f}% "
                    f"({prev_close:,.2f} → {close:,.2f}) on the latest snapshot.",
                    sec, market, ticker,
                    payload={"move_pct": round(move, 4), "close": close, "previous_close": prev_close},
                )
                if event:
                    events.append(event)
        if prev_vol > 0 and cur_vol >= prev_vol * vol_mult:
            key = f"volume_spike:{sec}:{day}"
            if not db.is_notification_processed(key):
                event = _emit(
                    db, key, IMPORTANT, "volume_spike",
                    f"{sec} VOLUME SPIKE {cur_vol / prev_vol:.1f}x",
                    f"{sec} traded {cur_vol / prev_vol:.1f}x its prior snapshot volume "
                    f"({prev_vol:,.0f} → {cur_vol:,.0f}).",
                    sec, market, ticker,
                    payload={"multiple": round(cur_vol / prev_vol, 2), "volume": cur_vol},
                )
                if event:
                    events.append(event)
    return events


# ---------------------------------------------------------------------------
# Important news (strong sentiment on freshly stored articles)
# ---------------------------------------------------------------------------


def _important_news_events(db: Database, now: datetime) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    cutoff = (now - _NEWS_FRESH_WINDOW).isoformat()
    for n in db.recent_news_feed(limit=200):
        if str(n.get("fetched_at") or "") < cutoff:
            continue
        market, ticker = str(n.get("market") or ""), str(n.get("ticker") or "")
        if not market or not ticker or market == "GLOBAL" or ticker == "NEWS":
            continue
        try:
            score = abs(float(n.get("sentiment_score") or 0.0))
        except (TypeError, ValueError):
            continue
        if score < 0.5:
            continue
        sec = f"{market}:{ticker}"
        url_key = hashlib.md5(str(n.get("url") or n.get("title") or "").encode()).hexdigest()[:12]
        key = f"important_news:{sec}:{url_key}"
        if db.is_notification_processed(key):
            continue
        label = str(n.get("sentiment_label") or ("bullish" if (n.get("sentiment_score") or 0) > 0 else "bearish"))
        event = _emit(
            db, key, IMPORTANT, "important_news",
            f"{sec} IMPORTANT NEWS ({label.upper()})",
            str(n.get("title") or ""),
            sec, market, ticker,
            payload={"sentiment": label, "score": round(float(n.get("sentiment_score") or 0.0), 4), "url": n.get("url") or ""},
        )
        if event:
            events.append(event)
    return events


# ---------------------------------------------------------------------------
# User-defined price targets
# ---------------------------------------------------------------------------


def _price_target_events(db: Database, now: datetime) -> list[dict[str, Any]]:
    """Fire active one-shot price rules against the latest stored snapshot."""
    events: list[dict[str, Any]] = []
    for rule in db.price_alerts(active_only=True):
        market = str(rule.get("market") or "")
        ticker = str(rule.get("ticker") or "")
        snapshot = db.latest_price_snapshot(market, ticker)
        if not snapshot or str(snapshot.get("data_status") or "ready") != "ready":
            continue
        try:
            current = float(snapshot.get("close") or 0.0)
            target = float(rule.get("target_price") or 0.0)
        except (TypeError, ValueError):
            continue
        if current <= 0 or target <= 0:
            continue
        direction = str(rule.get("direction") or "").lower()
        reached = current >= target if direction == "above" else current <= target
        if not reached:
            db.update_price_alert(int(rule["id"]), last_price=current)
            continue

        sec = f"{market}:{ticker}"
        # updated_at is the rule revision: stable while monitoring, changed
        # when the user re-arms it, so every arm can fire exactly once.
        key = f"price_target:{int(rule['id'])}:{rule['updated_at']}"
        verb = "rose to" if direction == "above" else "fell to"
        note = str(rule.get("note") or "").strip()
        event = _emit(
            db,
            key,
            HIGH,
            "price_target",
            f"{sec} PRICE TARGET REACHED",
            f"{sec} {verb} {current:,.2f}, crossing your {direction} target of "
            f"{target:,.2f}.{f' {note}' if note else ''}",
            sec,
            market,
            ticker,
            payload={
                "alert_id": int(rule["id"]),
                "direction": direction,
                "target_price": target,
                "current_price": current,
            },
        )
        if event:
            db.update_price_alert(
                int(rule["id"]), active=False, last_price=current,
                triggered_at=now.isoformat(),
            )
            events.append(event)
    return events


# ---------------------------------------------------------------------------
# Significant trade detection
# ---------------------------------------------------------------------------


def _trade_events(db: Database) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    threshold = float(settings.notification_trade_threshold)
    orders = db.paper_orders()
    # Group by security so position transitions can be computed incrementally.
    by_sec: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for o in orders:
        by_sec.setdefault((o["market"], o["ticker"]), []).append(o)

    for (market, ticker), olist in by_sec.items():
        direction: str | None = None
        prev_start: str | None = None  # direction at the START of the previous order
        qty = 0.0
        for o in olist:
            key = f"trade:{o['order_id']}"
            before = direction
            side = o["side"]
            q = float(o["quantity"])
            if side == "BUY":
                direction, qty = "LONG", qty + q
            elif side == "SHORT":
                direction, qty = "SHORT", qty + q
            elif side == "SELL":
                qty -= q
                if qty <= 1e-9:
                    direction = None
            elif side == "COVER":
                qty -= q
                if qty <= 1e-9:
                    direction = None
            elif side == "CLOSE":
                direction, qty = None, 0.0
            after = direction

            # A reversal is either a single order flipping direction (defensive)
            # or a close-then-open of the opposite side across two orders.
            reversed_ = (
                (before is not None and after is not None and before != after)
                or (
                    before is None
                    and prev_start in ("LONG", "SHORT")
                    and after is not None
                    and after != prev_start
                )
            )
            if reversed_:
                from_dir = before or prev_start
                key = f"trade:{o['order_id']}"
                if db.is_notification_processed(key):
                    prev_start = before
                    continue
                notional = q * float(o["price"])
                sec = f"{market}:{ticker}"
                db.mark_notification_processed(key)
                event = _emit(
                    db, key, HIGH, "position_reversed",
                    f"{sec} REVERSED {from_dir} → {after}",
                    f"Trader reversed {sec} from {from_dir} to {after} "
                    f"({o['side']} {q:,.0f} @ {float(o['price']):,.2f}, notional {_money(notional)}).",
                    sec, market, ticker,
                    payload={"before": from_dir, "after": after, "notional": notional, "side": o["side"]},
                )
                if event:
                    events.append(event)
                prev_start = before
                continue

            if db.is_notification_processed(key):
                prev_start = before
                continue  # already emitted (or already known to be below threshold)

            notional = q * float(o["price"])
            sec = f"{market}:{ticker}"
            if notional < threshold:
                db.mark_notification_processed(key)  # small trades stay silent but seen
                prev_start = before
                continue
            opened = before is None and after is not None
            closed = before is not None and after is None
            verb = "opened" if opened else "closed" if closed else "adjusted"
            db.mark_notification_processed(key)
            event = _emit(
                db, key, HIGH, "significant_trade",
                f"SIGNIFICANT TRADE - {sec}",
                f"Trader {verb} a {after or 'position'} in {sec}: "
                f"{o['side']} {q:,.0f} @ {float(o['price']):,.2f} (notional {_money(notional)}).",
                sec, market, ticker,
                payload={"side": o["side"], "notional": notional, "direction": after},
            )
            if event:
                events.append(event)
            prev_start = before
    return events


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scan(db: Database, now: datetime | None = None) -> dict[str, Any]:
    """Run the event detectors and persist any new notifications."""
    now = now or _now()
    new_events: list[dict[str, Any]] = []
    detectors = (
        _market_open_events,
        _committee_change_events,
        _signal_change_events,
        _market_activity_events,
        _important_news_events,
        _price_target_events,
        _trade_events,
    )
    for detector in detectors:
        try:
            new_events.extend(detector(db, now) if detector is not _trade_events else detector(db))
        except Exception as exc:
            logger.warning("Notification detector %s failed: %s", detector.__name__, exc)
    return {"new": new_events, "count": len(new_events)}


def recent(db: Database, limit: int = 50) -> list[dict[str, Any]]:
    return db.notifications(limit=limit)


def ack(db: Database, keys: list[str]) -> dict[str, Any]:
    return {"acked": db.ack_notifications(keys)}
