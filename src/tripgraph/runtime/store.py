"""SQLite TripGraph store with atomic version + request-generation commit fencing.

Guarantee (TripGraph v7):
- A timed-out, cancelled, or stale-version computation can NEVER mutate committed state.
- Every commit executes ONE atomic SQL UPDATE fenced by:
    WHERE trip_id = ?
      AND version = ?
      AND EXISTS (SELECT 1 FROM edit_requests
                  WHERE request_id = ? AND status = 'ACTIVE' AND deadline_utc > ?)
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass

from tripgraph.schema.models import TripGraph


class StaleVersionOrAbortedError(RuntimeError):
    """Raised when commit fencing rejects a stale, aborted, or expired edit."""


@dataclass(frozen=True)
class CommittedTrip:
    graph: TripGraph
    version: int
    last_request_id: str | None


class FencedTripStore:
    """Thread-safe SQLite store implementing v7 atomic commit fencing."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS trips (
                    trip_id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    last_request_id TEXT,
                    graph_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS edit_requests (
                    request_id TEXT PRIMARY KEY,
                    trip_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    deadline_utc REAL NOT NULL
                );
                """
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create_trip(self, graph: TripGraph) -> CommittedTrip:
        normalized = graph.model_copy(update={"version": 1})
        payload = normalized.model_dump_json()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO trips (trip_id, version, last_request_id, graph_json) VALUES (?, ?, NULL, ?)",
                (normalized.trip_id, 1, payload),
            )
        return CommittedTrip(graph=normalized, version=1, last_request_id=None)

    def get_trip(self, trip_id: str) -> CommittedTrip | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT version, last_request_id, graph_json FROM trips WHERE trip_id = ?",
                (trip_id,),
            ).fetchone()
        if row is None:
            return None
        graph = TripGraph.model_validate_json(row["graph_json"])
        return CommittedTrip(
            graph=graph,
            version=int(row["version"]),
            last_request_id=row["last_request_id"],
        )

    def register_request(
        self, request_id: str, trip_id: str, deadline_utc: float
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO edit_requests (request_id, trip_id, status, deadline_utc) VALUES (?, ?, 'ACTIVE', ?)",
                (request_id, trip_id, deadline_utc),
            )

    def abort_request(self, request_id: str) -> None:
        """Mark an in-flight edit request ABORTED immediately on timeout or cancellation."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE edit_requests SET status = 'ABORTED' WHERE request_id = ? AND status = 'ACTIVE'",
                (request_id,),
            )

    def commit_fenced(
        self,
        *,
        trip_id: str,
        base_version: int,
        request_id: str,
        new_graph: TripGraph,
        now_utc: float | None = None,
    ) -> CommittedTrip:
        """Atomically commit new_graph iff base_version matches AND request is ACTIVE & unexpired."""
        check_now = time.time() if now_utc is None else now_utc
        next_version = base_version + 1
        versioned_graph = new_graph.model_copy(update={"version": next_version})
        payload = versioned_graph.model_dump_json()

        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                UPDATE trips
                SET graph_json = ?, version = version + 1, last_request_id = ?
                WHERE trip_id = ?
                  AND version = ?
                  AND EXISTS (
                      SELECT 1 FROM edit_requests
                      WHERE request_id = ?
                        AND trip_id = ?
                        AND status = 'ACTIVE'
                        AND deadline_utc > ?
                  )
                """,
                (payload, request_id, trip_id, base_version, request_id, trip_id, check_now),
            )
            if cur.rowcount == 0:
                raise StaleVersionOrAbortedError(
                    f"Fenced commit rejected for trip={trip_id} base_version={base_version} req={request_id}"
                )
            self._conn.execute(
                "UPDATE edit_requests SET status = 'COMMITTED' WHERE request_id = ?",
                (request_id,),
            )

        return CommittedTrip(
            graph=versioned_graph,
            version=next_version,
            last_request_id=request_id,
        )
