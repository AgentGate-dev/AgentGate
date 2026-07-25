"""Audit log and human-approval queue for the orchestrator."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional


class OrchestratorStore:
    """SQLite-backed audit trail and pending approvals for escalations."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS audit_events ("
                "  id TEXT PRIMARY KEY,"
                "  created_at TEXT NOT NULL,"
                "  trace_id TEXT,"
                "  invoice_number TEXT,"
                "  gate_decision TEXT NOT NULL,"
                "  execution_status TEXT NOT NULL,"
                "  payment_id TEXT,"
                "  approval_id TEXT,"
                "  payload_json TEXT NOT NULL"
                ")"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS pending_approvals ("
                "  id TEXT PRIMARY KEY,"
                "  created_at TEXT NOT NULL,"
                "  invoice_number TEXT NOT NULL,"
                "  proposed_action_json TEXT NOT NULL,"
                "  source_json TEXT NOT NULL,"
                "  gate_trace_id TEXT,"
                "  status TEXT NOT NULL"
                ")"
            )
            self._conn.commit()

    def record_audit(self, event: dict[str, Any]) -> str:
        event_id = event.get("id") or str(uuid.uuid4())
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_events "
                "(id, created_at, trace_id, invoice_number, gate_decision, "
                "execution_status, payment_id, approval_id, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    event.get("created_at")
                    or datetime.now(timezone.utc).isoformat(),
                    event.get("trace_id"),
                    event.get("invoice_number"),
                    event["gate_decision"],
                    event["execution_status"],
                    event.get("payment_id"),
                    event.get("approval_id"),
                    json.dumps(event, default=str),
                ),
            )
            self._conn.commit()
        return event_id

    def create_pending_approval(
        self,
        *,
        invoice_number: str,
        proposed_action: dict[str, Any],
        source: dict[str, Any],
        gate_trace_id: Optional[str],
    ) -> str:
        approval_id = str(uuid.uuid4())
        with self._lock:
            self._conn.execute(
                "INSERT INTO pending_approvals "
                "(id, created_at, invoice_number, proposed_action_json, "
                "source_json, gate_trace_id, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    approval_id,
                    datetime.now(timezone.utc).isoformat(),
                    invoice_number,
                    json.dumps(proposed_action, default=str),
                    json.dumps(source, default=str),
                    gate_trace_id,
                    "pending",
                ),
            )
            self._conn.commit()
        return approval_id

    def get_pending_approval(self, approval_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, invoice_number, proposed_action_json, source_json, "
                "gate_trace_id, status, created_at FROM pending_approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "invoice_number": row[1],
            "proposed_action": json.loads(row[2]),
            "source": json.loads(row[3]),
            "gate_trace_id": row[4],
            "status": row[5],
            "created_at": row[6],
        }

    def resolve_approval(self, approval_id: str, *, approved: bool) -> bool:
        """First-resolution-wins: a conditional UPDATE that only fires while the
        row is still pending. Returns False when another resolution (or expiry)
        got there first — the caller must then execute nothing."""
        status = "approved" if approved else "rejected"
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE pending_approvals SET status = ? WHERE id = ? AND status = 'pending'",
                (status, approval_id),
            )
            self._conn.commit()
        return cursor.rowcount == 1

    def expire_approval(self, approval_id: str) -> bool:
        """Mark a stale pending approval expired (conditional, like resolve)."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE pending_approvals SET status = 'expired' "
                "WHERE id = ? AND status = 'pending'",
                (approval_id,),
            )
            self._conn.commit()
        return cursor.rowcount == 1

    def list_audit(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at, trace_id, invoice_number, gate_decision, "
                "execution_status, payment_id, approval_id "
                "FROM audit_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": row[0],
                "created_at": row[1],
                "trace_id": row[2],
                "invoice_number": row[3],
                "gate_decision": row[4],
                "execution_status": row[5],
                "payment_id": row[6],
                "approval_id": row[7],
            }
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
