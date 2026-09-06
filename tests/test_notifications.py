"""Tests for the terminal notification service + event detector (no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stock_alert_app import notifications
from stock_alert_app.db import Database


def _db(tmp_path):
    db = Database(tmp_path / "t.db")
    db.init_schema()
    return db


# A Sunday midday UTC when every configured market is closed -> no market-open
# noise bleeding into trade/committee tests.
_SILENT_NOW = datetime(2025, 3, 16, 12, 0, tzinfo=UTC)


def _add_order(db, order_id, side, qty, price, market="NYSE", ticker="T"):
    db.insert_paper_order(
        order_id=order_id, session_id="SESS-TEST", market=market, ticker=ticker,
        side=side, quantity=qty, price=price, fee=1.0,
        executed_at=datetime.now(UTC).isoformat(), decision_id=None, reason="test",
    )


def _types(db):
    return [n["type"] for n in db.notifications()]


class TestMarketOpen:
    def test_fires_once_per_session(self, tmp_path):
        db = _db(tmp_path)
        now = datetime(2025, 3, 10, 9, 0, tzinfo=UTC)  # 09:00 UTC -> LSE 09:00 (open)
        first = notifications.scan(db, now=now)
        assert any(e["type"] == "market_open" and e["market"] == "LSE" for e in first["new"])
        keys = [e["event_key"] for e in db.notifications() if e["market"] == "LSE"]
        assert len(keys) == 1
        assert keys[0] == "market_open:LSE:2025-03-10"

    def test_no_duplicate_on_rescan(self, tmp_path):
        db = _db(tmp_path)
        now = datetime(2025, 3, 10, 9, 0, tzinfo=UTC)
        notifications.scan(db, now=now)
        second = notifications.scan(db, now=now)  # simulated next refresh poll
        assert not any(e["type"] == "market_open" and e["market"] == "LSE" for e in second["new"])
        lse_events = [n for n in db.notifications() if n["market"] == "LSE"]
        assert len(lse_events) == 1

    def test_market_status_helper(self):
        from datetime import datetime as dt

        from stock_alert_app.config import settings
        from stock_alert_app.markets import load_markets, market_status

        m = load_markets(settings.markets_dir)["LSE"]
        # Tuesday 2025-03-11 09:30 UTC == 09:30 London (open 08:00-16:30)
        status = market_status(m, dt(2025, 3, 11, 9, 30, tzinfo=UTC))
        assert status["status"] == "open"
        # Weekend is closed.
        weekend = market_status(m, dt(2025, 3, 15, 9, 30, tzinfo=UTC))
        assert weekend["status"] == "closed" and weekend["is_weekend"] is True


class TestCommitteeChange:
    def test_reversal_fires_alert(self, tmp_path):
        db = _db(tmp_path)
        now = datetime.now(UTC)
        db.insert_verdict("NYSE", "T", "BULL", 0.8, 0.1, 0.2, 0.5, "reason1", lstm_score=0.5, technical_score=0.2)
        db.insert_verdict("NYSE", "T", "BEAR", 0.82, 0.1, -0.2, -0.5, "reason2", lstm_score=-0.5, technical_score=-0.2)
        res = notifications.scan(db, now=now)
        reversals = [e for e in res["new"] if e["type"] == "committee_reversal" and e["market"] == "NYSE"]
        assert reversals, f"expected reversal, got {[e['type'] for e in res['new']]}"
        assert reversals[0]["severity"] == "HIGH"
        assert "BULL" in reversals[0]["title"] and "BEAR" in reversals[0]["title"]

    def test_reversal_not_duplicated(self, tmp_path):
        db = _db(tmp_path)
        now = datetime.now(UTC)
        db.insert_verdict("NYSE", "T", "BULL", 0.8, 0.1, 0.2, 0.5, "r1", technical_score=0.2)
        db.insert_verdict("NYSE", "T", "BEAR", 0.82, 0.1, -0.2, -0.5, "r2", technical_score=-0.2)
        notifications.scan(db, now=now)
        second = notifications.scan(db, now=now)
        assert not [e for e in second["new"] if e["type"] == "committee_reversal"]
        revs = [n for n in db.notifications() if n["type"] == "committee_reversal"]
        assert len(revs) == 1

    def test_same_verdict_is_not_a_change(self, tmp_path):
        db = _db(tmp_path)
        now = datetime.now(UTC)
        db.insert_verdict("NYSE", "T", "BULL", 0.7, 0.1, 0.2, 0.5, "r1", technical_score=0.2)
        db.insert_verdict("NYSE", "T", "BULL", 0.8, 0.1, 0.3, 0.6, "r2", technical_score=0.3)
        res = notifications.scan(db, now=now)
        assert not [e for e in res["new"] if e["type"].startswith("committee")]


class TestPriceTargets:
    def test_above_target_fires_once_and_disarms(self, tmp_path):
        db = _db(tmp_path)
        rule = db.create_price_alert("NYSE", "AAPL", "above", 120.0, "Breakout level")
        db.insert_price_snapshot("NYSE", "AAPL", close=110.0)
        assert not [e for e in notifications.scan(db, now=_SILENT_NOW)["new"] if e["type"] == "price_target"]

        db.insert_price_snapshot("NYSE", "AAPL", close=121.5)
        events = [e for e in notifications.scan(db, now=_SILENT_NOW)["new"] if e["type"] == "price_target"]
        assert len(events) == 1
        assert events[0]["severity"] == "HIGH"
        assert events[0]["payload"]["target_price"] == 120.0
        stored = next(a for a in db.price_alerts() if a["id"] == rule["id"])
        assert stored["active"] == 0
        assert stored["triggered_at"]
        assert not [e for e in notifications.scan(db, now=_SILENT_NOW)["new"] if e["type"] == "price_target"]

    def test_below_target_can_be_rearmed(self, tmp_path):
        db = _db(tmp_path)
        rule = db.create_price_alert("LSE", "ULVR", "below", 40.0)
        db.insert_price_snapshot("LSE", "ULVR", close=39.0)
        assert len([e for e in notifications.scan(db, now=_SILENT_NOW)["new"] if e["type"] == "price_target"]) == 1
        db.update_price_alert(rule["id"], active=True)
        assert len([e for e in notifications.scan(db, now=_SILENT_NOW)["new"] if e["type"] == "price_target"]) == 1


class TestTradeEvents:
    def test_significant_trade_fires(self, tmp_path):
        db = _db(tmp_path)
        _add_order(db, "O1", "BUY", 100, 1000.0)  # notional 100,000 >= 25,000
        res = notifications.scan(db, now=_SILENT_NOW)
        sig = [e for e in res["new"] if e["type"] == "significant_trade"]
        assert len(sig) == 1
        assert sig[0]["severity"] == "HIGH"
        assert sig[0]["payload"]["notional"] == 100000.0

    def test_small_trades_stay_silent(self, tmp_path):
        db = _db(tmp_path)
        for i in range(3):
            _add_order(db, f"O{i}", "BUY", 1, 100.0)  # notional 100 each
        res = notifications.scan(db, now=_SILENT_NOW)
        assert not [e for e in res["new"] if e["type"] == "significant_trade"]
        # They were still marked processed (no flood on rescan).
        second = notifications.scan(db, now=_SILENT_NOW)
        assert not second["new"]
        assert db.notifications() == []

    def test_position_reversed_long_to_short(self, tmp_path):
        db = _db(tmp_path)
        _add_order(db, "O1", "BUY", 100, 10.0)
        _add_order(db, "O2", "SELL", 100, 10.0)
        _add_order(db, "O3", "SHORT", 100, 10.0)  # close-then-open opposite
        res = notifications.scan(db, now=_SILENT_NOW)
        rev = [e for e in res["new"] if e["type"] == "position_reversed"]
        assert len(rev) == 1
        assert rev[0]["severity"] == "HIGH"
        assert rev[0]["payload"]["before"] == "LONG"
        assert rev[0]["payload"]["after"] == "SHORT"

    def test_large_trade_deduplicated_on_rescan(self, tmp_path):
        db = _db(tmp_path)
        _add_order(db, "O1", "BUY", 100, 1000.0)
        notifications.scan(db)
        second = notifications.scan(db, now=_SILENT_NOW)
        assert not [e for e in second["new"] if e["type"] == "significant_trade"]
        assert len([n for n in db.notifications() if n["type"] == "significant_trade"]) == 1


class TestScanIdempotence:
    def test_repeated_scans_are_quiet(self, tmp_path):
        db = _db(tmp_path)
        now = datetime(2025, 3, 10, 9, 0, tzinfo=UTC)
        db.insert_verdict("NYSE", "T", "BULL", 0.8, 0.1, 0.2, 0.5, "r1", technical_score=0.2)
        db.insert_verdict("NYSE", "T", "BEAR", 0.82, 0.1, -0.2, -0.5, "r2", technical_score=-0.2)
        _add_order(db, "O1", "BUY", 100, 1000.0)
        first = notifications.scan(db, now=now)
        assert first["count"] >= 2  # committee reversal + significant trade (+ maybe market open)
        second = notifications.scan(db, now=now)
        assert second["count"] == 0
