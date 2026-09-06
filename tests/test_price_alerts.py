from __future__ import annotations

import pytest

from stock_alert_app.db import Database


@pytest.fixture
def client(tmp_path, monkeypatch):
    import stock_alert_app.web_app as web_app
    from fastapi.testclient import TestClient

    db = Database(tmp_path / "alerts.db")
    db.init_schema()
    db.insert_price_snapshot("NYSE", "AAPL", close=195.0)
    monkeypatch.setattr(web_app, "_db", lambda: db)
    return TestClient(web_app.app), db


def test_price_alert_crud_and_quote_enrichment(client):
    api, _ = client
    created = api.post(
        "/api/price-alerts",
        json={
            "market": "nyse",
            "ticker": "aapl",
            "direction": "above",
            "target_price": 200,
            "note": "Prior high",
        },
    )
    assert created.status_code == 200, created.text
    alert = created.json()
    assert alert["market"] == "NYSE" and alert["ticker"] == "AAPL"
    assert alert["current_price"] == 195.0
    assert alert["distance_pct"] == pytest.approx(5 / 195)

    listed = api.get("/api/price-alerts").json()
    assert len(listed) == 1 and listed[0]["active"] is True

    paused = api.patch(f"/api/price-alerts/{alert['id']}", json={"active": False})
    assert paused.status_code == 200 and paused.json()["active"] is False
    assert api.delete(f"/api/price-alerts/{alert['id']}").json()["removed"] is True
    assert api.get("/api/price-alerts").json() == []


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({"market": "NYSE", "ticker": "AAPL", "direction": "sideways", "target_price": 10}, "direction"),
        ({"market": "NYSE", "ticker": "AAPL", "direction": "above", "target_price": 0}, "target price"),
    ],
)
def test_price_alert_validation(client, payload, detail):
    api, _ = client
    response = api.post("/api/price-alerts", json=payload)
    assert response.status_code == 422
    assert detail in response.json()["detail"]
