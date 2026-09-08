from __future__ import annotations

import dataclasses
import json

import pytest

from stock_alert_app import paper
from stock_alert_app.analysis import stock_analysis
from stock_alert_app.db import Database


def _db(tmp_path):
    db = Database(tmp_path / "t.db")
    db.init_schema()
    return db


def _analysis_dict():
    return stock_analysis(
        {
            "market": "NYSE",
            "ticker": "T",
            "verdict": "BULL",
            "confidence": 0.6,
            "news_score": 0.3,
            "price_score": 0.2,
            "combined_score": 0.4,
            "reason": "News: bullish (5 articles, score +0.30); Signal agreement: moderate",
            "decided_at": "2026-01-01T10:00:00",
            "lstm_score": 0.4,
            "lstm_probability_up": 0.7,
            "lstm_predicted_return": 0.01,
            "lstm_confidence": 0.6,
            "technical_score": 0.2,
            "signals": "",
        }
    )


class TestDecisionSnapshots:
    def test_security_id_present(self, tmp_path):
        db = _db(tmp_path)
        did = paper.record_decision_snapshot(db, "NYSE", "T", _analysis_dict())
        assert did is not None
        snap = db.decision_snapshots()[0]
        assert snap["security_id"] == "NYSE:T"
        decision = json.loads(snap["decision_json"])
        assert decision["security_id"] == "NYSE:T"

    def test_snapshots_are_immutable(self, tmp_path):
        db = _db(tmp_path)
        paper.record_decision_snapshot(db, "NYSE", "T", _analysis_dict())
        first = db.decision_snapshots()
        # Re-recording with the same decided_at must not overwrite / duplicate.
        inserted = db.insert_decision_snapshot(
            "DEC-OTHER", "NYSE", "T", "2026-01-01T10:00:00", "BULL", 0.5, None, None, "{}"
        )
        assert inserted is False
        assert len(db.decision_snapshots()) == len(first)

    def test_new_decision_creates_new_snapshot(self, tmp_path):
        db = _db(tmp_path)
        d1 = _analysis_dict()
        paper.record_decision_snapshot(db, "NYSE", "T", d1)
        d2 = _analysis_dict()
        d2["decided_at"] = "2026-01-01T11:00:00"
        paper.record_decision_snapshot(db, "NYSE", "T", d2)
        assert len(db.decision_snapshots(ticker="T")) == 2


class TestEvaluation:
    def test_only_post_decision_prices_used(self):
        snap = {"decided_at": "2026-01-01T10:00:00", "verdict": "BULL"}
        # A bar BEFORE the decision must never be used as the reference.
        bars = [
            {"date": "2026-01-01 09:30", "close": 90.0},
            {"date": "2026-01-01 10:05", "close": 100.12},
            {"date": "2026-01-01 10:15", "close": 100.31},
            {"date": "2026-01-01 11:00", "close": 100.67},
        ]
        r = paper.evaluate_snapshot(snap, bars)
        assert r["status"] == "ok"
        assert r["reference_price"] == 100.12  # not the 09:30 bar
        assert r["prices"]["p60"] == 100.67
        assert r["correct"] == 1

    def test_missing_forward_data_is_no_data(self):
        snap = {"decided_at": "2026-01-01T10:00:00", "verdict": "BULL"}
        r = paper.evaluate_snapshot(snap, [{"date": "2026-01-01 09:30", "close": 90.0}])
        assert r["status"] == "no_data"
        assert r["reference_price"] is None

    def test_no_look_ahead_on_direction(self):
        # BULL decision; only bars after the timestamp count. Late bar is lower.
        snap = {"decided_at": "2026-01-01T10:00:00", "verdict": "BULL"}
        bars = [
            {"date": "2026-01-01 10:05", "close": 100.0},
            {"date": "2026-01-01 11:00", "close": 99.0},
        ]
        r = paper.evaluate_snapshot(snap, bars)
        assert r["correct"] == 0  # fell after decision -> BULL wrong


class TestPaperEngine:
    """Fincept-style pt_* engine: multi-portfolio, margin blocking, netting."""

    def _setup(self, tmp_path, monkeypatch, price=100.0, fee_rate=0.0, leverage=1.0):
        monkeypatch.setattr(paper, "_execution_price", lambda db, sym, m, t: price)
        monkeypatch.setattr(paper, "settings", dataclasses.replace(paper.settings, paper_slippage=0.0))
        db = _db(tmp_path)
        p = paper.pt_create_portfolio(db, "Main", 100000.0, fee_rate=fee_rate, leverage=leverage)
        return db, p

    def test_buy_market_fills_and_opens_long(self, tmp_path, monkeypatch):
        db, p = self._setup(tmp_path, monkeypatch, price=100.0)
        o = paper.pt_place_order(db, p["id"], "NYSE", "T", "buy", "market", 10.0, exchange="NYSE")
        assert o["status"] == "filled" and o["filled_qty"] == 10.0
        state = paper.pt_portfolio_state(db, p["id"], record_equity=False)
        pos = state["positions"][0]
        assert pos["side"] == "long" and pos["quantity"] == 10.0
        # leverage 1x -> margin = full notional = 10*100 = 1000, blocked from cash.
        assert state["cash"] == pytest.approx(100000.0 - 1000.0)
        assert pos["held_margin"] == pytest.approx(1000.0)
        assert state["equity"] == pytest.approx(state["cash"] + state["long_value"])

    def test_close_releases_margin_and_realizes_pnl(self, tmp_path, monkeypatch):
        prices = iter([100.0, 110.0])
        monkeypatch.setattr(paper, "_execution_price", lambda db, sym, m, t: next(prices))
        monkeypatch.setattr(paper, "settings", dataclasses.replace(paper.settings, paper_slippage=0.0))
        db = _db(tmp_path)
        p = paper.pt_create_portfolio(db, "Main", 100000.0, fee_rate=0.0)
        paper.pt_place_order(db, p["id"], "NYSE", "T", "buy", "market", 10.0, exchange="NYSE")
        paper.pt_place_order(db, p["id"], "NYSE", "T", "sell", "market", 10.0, exchange="NYSE")
        state = paper.pt_portfolio_state(db, p["id"], record_equity=False)
        assert state["positions"] == []
        # realized = (110 - 100) * 10 = 100; cash returns the blocked margin + pnl.
        assert state["realized_pnl"] == pytest.approx(100.0)
        assert state["cash"] == pytest.approx(100100.0)

    def test_short_and_cover(self, tmp_path, monkeypatch):
        db, p = self._setup(tmp_path, monkeypatch, price=100.0)
        paper.pt_place_order(db, p["id"], "NYSE", "T", "sell", "market", 10.0, exchange="NYSE")
        state = paper.pt_portfolio_state(db, p["id"], record_equity=False)
        assert state["positions"][0]["side"] == "short" and state["positions"][0]["quantity"] == 10.0
        paper.pt_place_order(db, p["id"], "NYSE", "T", "buy", "market", 10.0, exchange="NYSE")
        assert paper.pt_portfolio_state(db, p["id"], record_equity=False)["positions"] == []

    def test_netting_sell_closes_long_opens_short(self, tmp_path, monkeypatch):
        db, p = self._setup(tmp_path, monkeypatch, price=100.0)
        paper.pt_place_order(db, p["id"], "NYSE", "T", "buy", "market", 10.0, exchange="NYSE")
        # Sell 15: closes the 10-long and opens a 5-short in one fill.
        paper.pt_place_order(db, p["id"], "NYSE", "T", "sell", "market", 15.0, exchange="NYSE")
        state = paper.pt_portfolio_state(db, p["id"], record_equity=False)
        assert len(state["positions"]) == 1
        pos = state["positions"][0]
        assert pos["side"] == "short" and pos["quantity"] == 5.0

    def test_insufficient_margin_is_rejected_and_recorded(self, tmp_path, monkeypatch):
        db, p = self._setup(tmp_path, monkeypatch, price=1000.0)
        with pytest.raises(ValueError):
            paper.pt_place_order(db, p["id"], "NYSE", "T", "buy", "market", 1000.0, exchange="NYSE")
        orders = db.pt_get_orders(p["id"])
        assert any(o["status"] == "rejected" for o in orders)

    def test_invalid_side_quantity_and_order_type(self, tmp_path, monkeypatch):
        db, p = self._setup(tmp_path, monkeypatch, price=100.0)
        with pytest.raises(ValueError):
            paper.pt_place_order(db, p["id"], "NYSE", "T", "long", "market", 10.0)
        with pytest.raises(ValueError):
            paper.pt_place_order(db, p["id"], "NYSE", "T", "buy", "market", -1.0)
        with pytest.raises(ValueError):
            paper.pt_place_order(db, p["id"], "NYSE", "T", "buy", "banana", 10.0)
        with pytest.raises(ValueError):
            paper.pt_place_order(db, p["id"], "NYSE", "T", "buy", "limit", 10.0)  # no price

    def test_limit_order_rests_then_fills_on_cross(self, tmp_path, monkeypatch):
        prices = iter([100.0, 90.0, 90.0])
        monkeypatch.setattr(paper, "_execution_price", lambda db, sym, m, t: next(prices))
        monkeypatch.setattr(paper, "settings", dataclasses.replace(paper.settings, paper_slippage=0.0))
        db = _db(tmp_path)
        p = paper.pt_create_portfolio(db, "Main", 100000.0, fee_rate=0.0)
        o = paper.pt_place_order(db, p["id"], "NYSE", "T", "buy", "limit", 10.0, price=95.0, exchange="NYSE")
        assert o["status"] == "pending"  # market 100 > limit 95 -> not filled
        filled = paper.pt_process_pending_orders(db, p["id"])
        assert filled == 0
        # price now 90 (<=95) -> trigger on the next process pass.
        filled = paper.pt_process_pending_orders(db, p["id"])
        assert filled == 1
        assert db.pt_get_order(o["id"])["status"] == "filled"

    def test_cancel_releases_margin(self, tmp_path, monkeypatch):
        db, p = self._setup(tmp_path, monkeypatch, price=100.0)
        o = paper.pt_place_order(db, p["id"], "NYSE", "T", "buy", "limit", 10.0, price=90.0, exchange="NYSE")
        assert db.pt_get_margin_block(o["id"]) == pytest.approx(900.0)
        cash_before = paper.pt_get_portfolio(db, p["id"])["balance"]
        paper.pt_cancel_order(db, o["id"])
        assert db.pt_get_order(o["id"])["status"] == "cancelled"
        assert paper.pt_get_portfolio(db, p["id"])["balance"] == pytest.approx(cash_before + 900.0)

    def test_quote_no_data(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paper, "_execution_price", lambda db, sym, m, t: None)
        db = _db(tmp_path)
        q = paper.pt_quote(db, "NYSE", "T")
        assert q["status"] == "no_data" and q["price"] is None

    def test_ensure_default_portfolio_autocreates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paper, "_execution_price", lambda db, sym, m, t: 100.0)
        db = _db(tmp_path)
        p1 = paper.ensure_default_portfolio(db, user_id="u1")
        p2 = paper.ensure_default_portfolio(db, user_id="u1")
        assert p1["id"] == p2["id"]  # idempotent
        assert p1["name"] == "Main"

    def test_leverage_reduces_required_margin(self, tmp_path, monkeypatch):
        db, p = self._setup(tmp_path, monkeypatch, price=100.0, leverage=5.0, fee_rate=0.0)
        paper.pt_place_order(db, p["id"], "NYSE", "T", "buy", "market", 10.0, exchange="NYSE", product="MIS")
        state = paper.pt_portfolio_state(db, p["id"], record_equity=False)
        # notional 1000 / leverage 5 = 200 blocked (intraday MIS uses portfolio leverage).
        assert state["cash"] == pytest.approx(100000.0 - 200.0)
        assert state["positions"][0]["held_margin"] == pytest.approx(200.0)

    def test_portfolio_crud(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paper, "_execution_price", lambda db, sym, m, t: 100.0)
        db = _db(tmp_path)
        p = paper.pt_create_portfolio(db, "Alpha", 50000.0, user_id="u1")
        assert len(paper.pt_list_portfolios(db, "u1")) == 1
        paper.pt_set_balance(db, p["id"], 75000.0)
        assert paper.pt_get_portfolio(db, p["id"])["balance"] == 75000.0
        paper.pt_delete_portfolio(db, p["id"])
        assert paper.pt_list_portfolios(db, "u1") == []

    def test_reset_preserves_account_identity_and_clears_trading_state(self, tmp_path, monkeypatch):
        db, p = self._setup(tmp_path, monkeypatch, price=100.0)
        paper.pt_place_order(db, p["id"], "NYSE", "T", "buy", "market", 10.0)

        reset = paper.pt_reset_portfolio(db, p["id"])

        assert reset["id"] == p["id"]
        assert reset["name"] == p["name"]
        assert reset["balance"] == pytest.approx(p["initial_balance"])
        assert db.pt_get_orders(p["id"]) == []
        assert db.pt_get_positions(p["id"]) == []
        assert db.pt_get_trades(p["id"]) == []

    def test_equity_and_buying_power_use_margin_accounting(self, tmp_path, monkeypatch):
        db, p = self._setup(tmp_path, monkeypatch, price=100.0, leverage=5.0)
        paper.pt_place_order(db, p["id"], "NYSE", "T", "sell", "market", 10.0, product="MIS")

        state = paper.pt_portfolio_state(db, p["id"], record_equity=False)

        assert state["cash"] == pytest.approx(99800.0)
        assert state["buying_power"] == pytest.approx(99800.0)
        assert state["equity"] == pytest.approx(100000.0)

    def test_reduce_only_rejects_missing_or_excess_shares(self, tmp_path, monkeypatch):
        db, p = self._setup(tmp_path, monkeypatch, price=100.0)
        with pytest.raises(ValueError, match="Insufficient shares"):
            paper.pt_place_order(db, p["id"], "NYSE", "T", "sell", "market", 1.0, reduce_only=True)
        paper.pt_place_order(db, p["id"], "NYSE", "T", "buy", "market", 2.0)
        with pytest.raises(ValueError, match="Insufficient shares"):
            paper.pt_place_order(db, p["id"], "NYSE", "T", "sell", "market", 3.0, reduce_only=True)
        assert len(db.pt_get_orders(p["id"], "rejected")) == 2

    def test_completed_order_cannot_be_cancelled_or_overfilled(self, tmp_path, monkeypatch):
        db, p = self._setup(tmp_path, monkeypatch, price=100.0)
        order = paper.pt_place_order(db, p["id"], "NYSE", "T", "buy", "market", 2.0)
        with pytest.raises(ValueError, match="not cancellable"):
            paper.pt_cancel_order(db, order["id"])
        assert db.pt_get_order(order["id"])["status"] == "filled"

        pending = paper.pt_place_order(db, p["id"], "NYSE", "T", "buy", "limit", 2.0, price=90.0)
        with pytest.raises(ValueError, match="remaining"):
            paper.pt_fill_order(db, pending["id"], 90.0, fill_qty=3.0)

    def test_limit_orders_fill_only_at_market_price_on_favorable_cross(self, tmp_path, monkeypatch):
        prices = iter([94.0, 106.0])
        monkeypatch.setattr(paper, "_execution_price", lambda db, sym, m, t: next(prices))
        monkeypatch.setattr(paper, "settings", dataclasses.replace(paper.settings, paper_slippage=0.0))
        db = _db(tmp_path)
        p = paper.pt_create_portfolio(db, "Main", 100000.0, fee_rate=0.0)
        buy = paper.pt_place_order(db, p["id"], "NYSE", "T", "buy", "limit", 1.0, price=95.0)
        assert paper.pt_process_pending_orders(db, p["id"]) == 1
        assert db.pt_get_order(buy["id"])["avg_price"] == pytest.approx(94.0)
        sell = paper.pt_place_order(db, p["id"], "NYSE", "T", "sell", "limit", 1.0, price=105.0)
        assert paper.pt_process_pending_orders(db, p["id"]) == 1
        assert db.pt_get_order(sell["id"])["avg_price"] == pytest.approx(106.0)

    def test_partial_limit_order_remains_open_and_fills_remaining_quantity(self, tmp_path, monkeypatch):
        db, p = self._setup(tmp_path, monkeypatch, price=93.0)
        order = paper.pt_place_order(db, p["id"], "NYSE", "T", "buy", "limit", 2.0, price=95.0)
        paper.pt_fill_order(db, order["id"], 94.0, fill_qty=1.0)
        assert db.pt_get_order(order["id"])["status"] == "partial"

        assert paper.pt_process_pending_orders(db, p["id"]) == 1
        filled = db.pt_get_order(order["id"])
        assert filled["status"] == "filled"
        assert filled["filled_qty"] == pytest.approx(2.0)
        assert filled["avg_price"] == pytest.approx(93.5)

    def test_realized_and_unrealized_pnl_include_fees(self, tmp_path, monkeypatch):
        prices = iter([100.0, 110.0, 110.0])
        monkeypatch.setattr(paper, "_execution_price", lambda db, sym, m, t: next(prices))
        monkeypatch.setattr(paper, "settings", dataclasses.replace(paper.settings, paper_slippage=0.0))
        db = _db(tmp_path)
        p = paper.pt_create_portfolio(db, "Main", 100000.0, fee_rate=0.001)
        paper.pt_place_order(db, p["id"], "NYSE", "T", "buy", "market", 10.0)
        open_state = paper.pt_portfolio_state(db, p["id"], record_equity=False)
        assert open_state["unrealized_pnl"] == pytest.approx(100.0)
        assert open_state["realized_pnl"] == pytest.approx(-1.0)
        assert open_state["equity"] == pytest.approx(100099.0)

        paper.pt_place_order(db, p["id"], "NYSE", "T", "sell", "market", 10.0)
        closed = paper.pt_portfolio_state(db, p["id"], record_equity=False)
        assert closed["realized_pnl"] == pytest.approx(97.9)
        assert closed["equity"] == pytest.approx(100097.9)

    def test_market_order_uses_latest_price_without_synthetic_slippage(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paper, "_execution_price", lambda db, sym, m, t: 123.45)
        monkeypatch.setattr(paper, "settings", dataclasses.replace(paper.settings, paper_slippage=0.25))
        db = _db(tmp_path)
        p = paper.pt_create_portfolio(db, "Main", 100000.0, fee_rate=0.0)

        order = paper.pt_place_order(db, p["id"], "NYSE", "T", "buy", "market", 1.0)

        assert order["avg_price"] == pytest.approx(123.45)

    def test_stop_limit_does_not_fake_fill_through_limit(self, tmp_path, monkeypatch):
        prices = iter([105.0, 100.5])
        monkeypatch.setattr(paper, "_execution_price", lambda db, sym, m, t: next(prices))
        db = _db(tmp_path)
        p = paper.pt_create_portfolio(db, "Main", 100000.0, fee_rate=0.0)
        order = paper.pt_place_order(
            db, p["id"], "NYSE", "T", "buy", "stop_limit", 1.0, price=101.0, stop_price=100.0
        )

        assert paper.pt_process_pending_orders(db, p["id"]) == 0
        assert db.pt_get_order(order["id"])["status"] == "pending"
        assert paper.pt_process_pending_orders(db, p["id"]) == 1
        assert db.pt_get_order(order["id"])["avg_price"] == pytest.approx(100.5)


class TestPerformance:
    def test_metrics_derived(self, tmp_path):
        db = _db(tmp_path)
        d = _analysis_dict()
        paper.record_decision_snapshot(db, "NYSE", "T", d)
        # inject an evaluation for the snapshot
        snap = db.decision_snapshots()[0]
        db.insert_decision_evaluation(
            snap["decision_id"], 100.0, {"p5": 100.5, "p15": 100.5, "p30": 100.5, "p60": 100.5, "close": 100.5}, 1, "ok"
        )
        perf = paper.performance(db)
        assert perf["decisions"] >= 1
        assert perf["evaluated"] == 1
        assert perf["directional_accuracy"] == 1.0
        assert perf["conviction_buckets"] and perf["conviction_buckets"][0]["n"] >= 1
