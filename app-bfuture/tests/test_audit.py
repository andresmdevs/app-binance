from decimal import Decimal

from binance.audit import AuditLog, read_audit


def test_write_read(tmp_path):
    p = tmp_path / "orders.jsonl"
    log = AuditLog(p)
    log.record("order", {"symbol": "BTCUSDT", "qty": Decimal("0.001")}, {"orderId": 1})
    log.record("cancel", {"orderId": 1})
    rows = read_audit(p)
    assert len(rows) == 2
    assert rows[0]["action"] == "order"
    assert rows[0]["request"]["symbol"] == "BTCUSDT"
    assert rows[0]["request"]["qty"] == "0.001"  # Decimal serializado vía default=str


def test_read_missing(tmp_path):
    assert read_audit(tmp_path / "nope.jsonl") == []
