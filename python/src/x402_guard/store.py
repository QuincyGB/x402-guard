"""The guard's local store: check cache + spend ledger (sqlite).

Everything here lives on the BUYER's machine, in the buyer's own
file — their data, never sent anywhere. Three tables:

  endpoint_checks  one row per vetted endpoint: when the guard last
                   paid Recheck to look at it, what Recheck answered
                   (BASELINE / UNCHANGED / CHANGED), and the endpoint's
                   payment fingerprint from that look (amount, asset,
                   payTo, network, description/schema hashes).
  route_verdicts   one row per endpoint: the last RouteVerdict gate
                   verdict + trust score and when it was read.
  spend            an append-only ledger of every settled payment the
                   guard made or let through (target payments AND its
                   own check calls), feeding the daily caps.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS endpoint_checks (
    url TEXT PRIMARY KEY,
    checked_at REAL NOT NULL,
    status TEXT NOT NULL,
    fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS route_verdicts (
    url TEXT PRIMARY KEY,
    checked_at REAL NOT NULL,
    verdict TEXT NOT NULL,
    score REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS spend (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    url TEXT NOT NULL,
    units INTEGER NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS spend_date_idx ON spend (date);
CREATE INDEX IF NOT EXISTS spend_date_url_idx ON spend (date, url);
"""


def default_store_path() -> Path:
    return Path.home() / ".x402-guard" / "guard.db"


class GuardStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- Recheck cache ---------------------------------------------------

    def get_check(self, url: str) -> dict | None:
        """{checked_at, status, fingerprint} for `url`, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT checked_at, status, fingerprint FROM endpoint_checks "
                "WHERE url = ?",
                (url,),
            ).fetchone()
        if row is None:
            return None
        return {
            "checked_at": float(row[0]),
            "status": row[1],
            "fingerprint": json.loads(row[2]),
        }

    def save_check(self, url: str, status: str, fingerprint: dict, *, now: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO endpoint_checks (url, checked_at, status, fingerprint) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(url) DO UPDATE SET checked_at = excluded.checked_at, "
                "status = excluded.status, fingerprint = excluded.fingerprint",
                (url, float(now), status, json.dumps(fingerprint, sort_keys=True)),
            )
            self._conn.commit()

    # -- RouteVerdict gate cache ------------------------------------------

    def get_route_verdict(self, url: str) -> dict | None:
        """{checked_at, verdict, score} for `url`, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT checked_at, verdict, score FROM route_verdicts WHERE url = ?",
                (url,),
            ).fetchone()
        if row is None:
            return None
        return {"checked_at": float(row[0]), "verdict": row[1], "score": float(row[2])}

    def save_route_verdict(
        self, url: str, verdict: str, score: float, *, now: float
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO route_verdicts (url, checked_at, verdict, score) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(url) DO UPDATE SET checked_at = excluded.checked_at, "
                "verdict = excluded.verdict, score = excluded.score",
                (url, float(now), verdict, float(score)),
            )
            self._conn.commit()

    # -- spend ledger -------------------------------------------------------

    @staticmethod
    def _date_of(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()

    def record_spend(self, url: str, units: int, *, now: float) -> None:
        if units <= 0:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO spend (date, url, units, ts) VALUES (?, ?, ?, ?)",
                (self._date_of(now), url, int(units), float(now)),
            )
            self._conn.commit()

    def spent_today(self, *, now: float, url: str | None = None) -> int:
        """Total settled units on the UTC day containing `now`,
        globally or for one paid URL."""
        date = self._date_of(now)
        with self._lock:
            if url is None:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(units), 0) FROM spend WHERE date = ?",
                    (date,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(units), 0) FROM spend "
                    "WHERE date = ? AND url = ?",
                    (date, url),
                ).fetchone()
        return int(row[0])
