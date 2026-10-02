"""Tests for analytics/calibration.py — v2 P(TP) calibration (Э4)."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics.calibration import (
    MIN_SAMPLES,
    compute_brier_score,
    compute_ece,
    load_for_source,
    load_ptp_data,
)

SCHEMA = """
CREATE TABLE decision_traces (
    id INTEGER PRIMARY KEY,
    symbol TEXT, timeframe TEXT, signal_type TEXT,
    regime TEXT, direction TEXT,
    feature_snapshot TEXT, outcome TEXT, pnl_pct REAL,
    signal_id INTEGER, confidence REAL
);
CREATE TABLE signals (
    id INTEGER PRIMARY KEY,
    symbol TEXT, signal_type TEXT,
    confidence_v2_pct REAL, confidence_v2_factors TEXT,
    created_at TEXT
);
CREATE TABLE signal_outcomes (
    signal_id INTEGER, status TEXT, pnl_pct REAL
);
"""


def _mkdb(tmp_path: Path) -> Path:
    db = tmp_path / "calib.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return db


def _add_trace(conn, *, outcome="HIT_TP", p_tp=0.7, snapshot="auto",
               signal_id=1, pnl=1.5):
    if snapshot == "auto":
        snapshot = json.dumps({"p_tp": p_tp, "setup_type": "reversal"})
    conn.execute(
        "INSERT INTO decision_traces (symbol, timeframe, signal_type, regime, "
        "direction, feature_snapshot, outcome, pnl_pct, signal_id) "
        "VALUES ('BTC/USDT', '1h', 'BUY', 'trend', 'BUY', ?, ?, ?, ?)",
        (snapshot, outcome, pnl, signal_id),
    )


def _add_legacy(conn, *, signal_id, conf=62.0, status="HIT_TP"):
    conn.execute(
        "INSERT INTO signals (id, symbol, signal_type, confidence_v2_pct, created_at) "
        "VALUES (?, 'BTC/USDT', 'BUY', ?, '2026-01-01')",
        (signal_id, conf),
    )
    conn.execute(
        "INSERT INTO signal_outcomes (signal_id, status, pnl_pct) VALUES (?, ?, 1.0)",
        (signal_id, status),
    )


class TestLoadPtpData:
    def test_parses_snapshot_and_converts_to_percent(self, tmp_path):
        db = _mkdb(tmp_path)
        conn = sqlite3.connect(db)
        _add_trace(conn, outcome="HIT_TP", p_tp=0.7, signal_id=1)
        _add_trace(conn, outcome="HIT_SL", p_tp=0.5, signal_id=2, pnl=-1.0)
        conn.commit()
        conn.close()

        data = load_ptp_data(db)
        assert len(data) == 2
        by_win = sorted(data, key=lambda d: d["confidence"])
        assert by_win[0]["confidence"] == pytest.approx(50.0)   # 0.5 → 50%
        assert by_win[0]["is_win"] == 0
        assert by_win[1]["confidence"] == pytest.approx(70.0)   # 0.7 → 70%
        assert by_win[1]["is_win"] == 1
        assert all(d["source"] == "p_tp" for d in data)

    def test_skips_non_terminal_outcomes_and_bad_rows(self, tmp_path):
        db = _mkdb(tmp_path)
        conn = sqlite3.connect(db)
        _add_trace(conn, outcome="HIT_TP", p_tp=0.6, signal_id=1)
        _add_trace(conn, outcome="EXPIRED", p_tp=0.6, signal_id=2)     # not terminal
        _add_trace(conn, outcome="HIT_SL", p_tp=0.0, signal_id=3)      # p_tp out of (0,1]
        _add_trace(conn, outcome="HIT_SL", snapshot="not json", signal_id=4)
        _add_trace(conn, outcome="HIT_SL",
                   snapshot=json.dumps({"components": 3}), signal_id=5)  # no p_tp
        conn.commit()
        conn.close()

        data = load_ptp_data(db)
        assert len(data) == 1
        assert data[0]["id"] == 1

    def test_empty_db_returns_nothing(self, tmp_path):
        db = _mkdb(tmp_path)
        assert load_ptp_data(db) == []


class TestSourceSelection:
    def test_auto_prefers_ptp_once_enough_samples(self, tmp_path):
        db = _mkdb(tmp_path)
        conn = sqlite3.connect(db)
        for i in range(MIN_SAMPLES):
            _add_trace(conn, p_tp=0.6, signal_id=i + 1)
        for i in range(3):
            _add_legacy(conn, signal_id=100 + i)
        conn.commit()
        conn.close()

        data, used = load_for_source(db, "auto")
        assert used == "p_tp"
        assert len(data) == MIN_SAMPLES

    def test_auto_falls_back_to_legacy_while_ptp_thin(self, tmp_path):
        db = _mkdb(tmp_path)
        conn = sqlite3.connect(db)
        _add_trace(conn, p_tp=0.6, signal_id=1)
        _add_trace(conn, outcome="HIT_SL", p_tp=0.5, signal_id=2, pnl=-1.0)
        for i in range(8):
            _add_legacy(conn, signal_id=100 + i)
        conn.commit()
        conn.close()

        data, used = load_for_source(db, "auto")
        assert used == "confidence_v2"
        assert len(data) == 8

    def test_explicit_ptp_never_falls_back(self, tmp_path):
        db = _mkdb(tmp_path)
        conn = sqlite3.connect(db)
        _add_trace(conn, p_tp=0.6, signal_id=1)
        for i in range(8):
            _add_legacy(conn, signal_id=100 + i)
        conn.commit()
        conn.close()

        data, used = load_for_source(db, "ptp")
        assert used == "p_tp"
        assert len(data) == 1


class TestMetrics:
    def test_perfectly_calibrated_data_has_low_ece(self, tmp_path):
        # 100 rows: confidence 50%, exactly half of them win → ECE ≈ 0
        data = [{"confidence": 50.0, "is_win": 1 if i % 2 == 0 else 0,
                 "pnl_pct": 0.0} for i in range(100)]
        assert compute_ece(data, n_bins=10) < 5.0

    def test_perfect_predictions_brier_zero(self):
        data = ([{"confidence": 99.0, "is_win": 1, "pnl_pct": 1.0}] * 50 +
                [{"confidence": 1.0, "is_win": 0, "pnl_pct": -1.0}] * 50)
        assert compute_brier_score(data) == pytest.approx(0.0, abs=0.01)
