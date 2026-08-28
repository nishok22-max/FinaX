"""SQLite persistence for proposals, tuning requests, audit trail, and chat threads.

Why a database at all, when the keeper's own position registry is a dict? Because the things
stored here outlive a process by design: an approval queue that forgets its pending items on
restart is not an approval queue, and an audit trail that cannot answer "what did the agent
propose last Tuesday, and who approved it" is not an audit trail.

Stdlib ``sqlite3`` only — no ORM, no migration tool, no new dependency. The schema is created on
first connect, so **no file exists until the agent is actually used**; a run with the layer
disabled leaves the checkout untouched.

Concurrency: ``sqlite3`` is blocking, and this process runs an asyncio event loop that must stay
responsive to the keeper's own RPC work. Every statement therefore crosses
:func:`asyncio.to_thread`; writers are serialised by an :class:`asyncio.Lock` so the single
connection is never used by two threads at once. WAL mode lets readers proceed against that
writer, and ``isolation_level=None`` (autocommit) guarantees no transaction is ever held open
across an ``await``.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from app.agent.models import (
    AuditAction,
    AuditRow,
    ChatMessageRow,
    DecisionRow,
    PolicyDecision,
    ProposalRow,
    ProposalStatus,
    TunableField,
    TuningRow,
    TuningStatus,
)

SCHEMA_VERSION = 1

_PRAGMAS = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS proposals (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id         TEXT    NOT NULL,
  borrower       TEXT    NOT NULL,
  created_at     REAL    NOT NULL,
  expires_at     REAL    NOT NULL,
  status         TEXT    NOT NULL,
  strategy       TEXT    NOT NULL,
  facts_json     TEXT    NOT NULL,
  gate_json      TEXT    NOT NULL,
  rationale      TEXT    NOT NULL,
  guard_flagged  INTEGER NOT NULL DEFAULT 0,
  decided_at     REAL,
  decided_by     TEXT,
  decision_note  TEXT,
  tx_hash        TEXT,
  result_json    TEXT
);
CREATE INDEX IF NOT EXISTS ix_proposals_status ON proposals(status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_proposals_borrow ON proposals(borrower, created_at DESC);

CREATE TABLE IF NOT EXISTS tuning_suggestions (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id              TEXT    NOT NULL,
  borrower            TEXT    NOT NULL,
  created_at          REAL    NOT NULL,
  field_name          TEXT    NOT NULL,
  current_value       INTEGER NOT NULL,
  suggested_value     INTEGER NOT NULL,
  rationale           TEXT    NOT NULL,
  eip712_payload_json TEXT    NOT NULL,
  status              TEXT    NOT NULL DEFAULT 'open',
  decided_at          REAL
);
CREATE INDEX IF NOT EXISTS ix_tuning_borrow ON tuning_suggestions(borrower, status);

CREATE TABLE IF NOT EXISTS decisions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      TEXT    NOT NULL,
  borrower    TEXT    NOT NULL,
  created_at  REAL    NOT NULL,
  node        TEXT    NOT NULL,
  latency_ms  INTEGER,
  llm_used    INTEGER NOT NULL DEFAULT 0,
  output_json TEXT    NOT NULL,
  error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_decisions_run ON decisions(run_id, id);

CREATE TABLE IF NOT EXISTS audit (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          REAL    NOT NULL,
  actor       TEXT    NOT NULL,
  action      TEXT    NOT NULL,
  borrower    TEXT,
  proposal_id INTEGER,
  detail_json TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_ts     ON audit(ts DESC);
CREATE INDEX IF NOT EXISTS ix_audit_borrow ON audit(borrower, ts DESC);

CREATE TABLE IF NOT EXISTS chat_threads (
  thread_id   TEXT PRIMARY KEY,
  created_at  REAL NOT NULL,
  last_active REAL NOT NULL,
  borrower    TEXT,
  title       TEXT
);

CREATE TABLE IF NOT EXISTS chat_messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id       TEXT    NOT NULL REFERENCES chat_threads(thread_id) ON DELETE CASCADE,
  ts              REAL    NOT NULL,
  role            TEXT    NOT NULL,
  content         TEXT    NOT NULL,
  tool_calls_json TEXT,
  facts_json      TEXT,
  guard_flagged   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_chat_thread ON chat_messages(thread_id, id);
"""


def _loads(raw: str | None) -> Any:
    return json.loads(raw) if raw else None


class AgentStore:
    """Async-safe wrapper over one long-lived SQLite connection."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()

    @property
    def path(self) -> str:
        return self._path

    @property
    def ready(self) -> bool:
        return self._conn is not None

    # --- Lifecycle -------------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the connection and create the schema. Idempotent and safe to race.

        Called from the lifespan, but also self-healing: any store method connects on demand, so
        the routes work in a bare ``TestClient`` that never ran the lifespan.
        """
        if self._conn is not None:
            return
        async with self._connect_lock:
            if self._conn is None:  # re-check: another coroutine may have won the lock
                self._conn = await asyncio.to_thread(self._open)

    def _open(self) -> sqlite3.Connection:
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because asyncio.to_thread hands work to a pool thread; safe
        # here because _write_lock serialises writers and reads are safe under WAL.
        conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(_PRAGMAS + _SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        return conn

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await asyncio.to_thread(conn.close)

    # --- Primitives ------------------------------------------------------------------------

    async def _rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        await self.connect()
        conn = self._conn
        assert conn is not None
        return await asyncio.to_thread(lambda: conn.execute(sql, args).fetchall())

    async def _row(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        rows = await self._rows(sql, args)
        return rows[0] if rows else None

    async def _write(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        await self.connect()
        conn = self._conn
        assert conn is not None
        async with self._write_lock:
            return await asyncio.to_thread(lambda: conn.execute(sql, args))

    # --- Proposals -------------------------------------------------------------------------

    async def insert_proposal(
        self,
        *,
        run_id: str,
        borrower: str,
        strategy: str,
        facts: dict[str, Any],
        gate: PolicyDecision,
        rationale: str,
        ttl_seconds: int,
        guard_flagged: bool = False,
        now: float | None = None,
    ) -> int:
        ts = time.time() if now is None else now
        cur = await self._write(
            "INSERT INTO proposals (run_id, borrower, created_at, expires_at, status, strategy,"
            " facts_json, gate_json, rationale, guard_flagged)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, borrower, ts, ts + ttl_seconds, ProposalStatus.PENDING.value, strategy,
             json.dumps(facts), gate.model_dump_json(), rationale, int(guard_flagged)),
        )
        return int(cur.lastrowid or 0)

    async def get_proposal(self, proposal_id: int) -> ProposalRow | None:
        row = await self._row("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        return None if row is None else self._proposal(row)

    async def list_proposals(
        self,
        *,
        status: ProposalStatus | None = None,
        borrower: str | None = None,
        limit: int = 20,
    ) -> list[ProposalRow]:
        sql = "SELECT * FROM proposals WHERE 1=1"
        args: list[Any] = []
        if status is not None:
            sql += " AND status = ?"
            args.append(status.value)
        if borrower is not None:
            sql += " AND borrower = ?"
            args.append(borrower)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [self._proposal(r) for r in await self._rows(sql, tuple(args))]

    async def claim_proposal(self, proposal_id: int, *, status: ProposalStatus) -> bool:
        """Atomically move a proposal out of ``PENDING``; ``True`` only for the winner.

        The guard against two operators approving the same proposal at once. ``UPDATE … WHERE
        status='PENDING'`` is a single statement, so exactly one caller can see ``rowcount == 1``
        — and only that caller goes on to execute. The keeper's in-flight lock is the second line
        of defence, but this one keeps the audit trail honest as well.
        """
        cur = await self._write(
            "UPDATE proposals SET status = ? WHERE id = ? AND status = ?",
            (status.value, proposal_id, ProposalStatus.PENDING.value),
        )
        return cur.rowcount == 1

    async def finish_proposal(
        self,
        proposal_id: int,
        *,
        status: ProposalStatus,
        decided_by: str | None = None,
        note: str | None = None,
        tx_hash: str | None = None,
        result: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> None:
        await self._write(
            "UPDATE proposals SET status = ?, decided_at = ?, decided_by = COALESCE(?, decided_by),"
            " decision_note = COALESCE(?, decision_note), tx_hash = COALESCE(?, tx_hash),"
            " result_json = COALESCE(?, result_json) WHERE id = ?",
            (status.value, time.time() if now is None else now, decided_by, note, tx_hash,
             json.dumps(result) if result is not None else None, proposal_id),
        )

    async def count_recent_proposals(
        self, *, since: float, borrower: str | None = None
    ) -> int:
        """Proposals created since ``since`` — the input to the gate's rate-limit checks."""
        if borrower is None:
            row = await self._row(
                "SELECT COUNT(*) AS n FROM proposals WHERE created_at >= ?", (since,)
            )
        else:
            row = await self._row(
                "SELECT COUNT(*) AS n FROM proposals WHERE created_at >= ? AND borrower = ?",
                (since, borrower),
            )
        return 0 if row is None else int(row["n"])

    async def expire_stale_proposals(self, *, now: float | None = None) -> int:
        ts = time.time() if now is None else now
        cur = await self._write(
            "UPDATE proposals SET status = ? WHERE status = ? AND expires_at < ?",
            (ProposalStatus.EXPIRED.value, ProposalStatus.PENDING.value, ts),
        )
        return cur.rowcount

    @staticmethod
    def _proposal(row: sqlite3.Row) -> ProposalRow:
        return ProposalRow(
            id=row["id"], run_id=row["run_id"], borrower=row["borrower"],
            created_at=row["created_at"], expires_at=row["expires_at"],
            status=ProposalStatus(row["status"]), strategy=row["strategy"],
            facts=json.loads(row["facts_json"]),
            gate=PolicyDecision.model_validate_json(row["gate_json"]),
            rationale=row["rationale"], guard_flagged=bool(row["guard_flagged"]),
            decided_at=row["decided_at"], decided_by=row["decided_by"],
            decision_note=row["decision_note"], tx_hash=row["tx_hash"],
            result=_loads(row["result_json"]),
        )

    # --- Tuning suggestions ----------------------------------------------------------------

    async def insert_tuning(
        self,
        *,
        run_id: str,
        borrower: str,
        field_name: TunableField,
        current_value: int,
        suggested_value: int,
        rationale: str,
        eip712_payload: dict[str, Any],
        now: float | None = None,
    ) -> int:
        cur = await self._write(
            "INSERT INTO tuning_suggestions (run_id, borrower, created_at, field_name,"
            " current_value, suggested_value, rationale, eip712_payload_json, status)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, borrower, time.time() if now is None else now, field_name, current_value,
             suggested_value, rationale, json.dumps(eip712_payload), TuningStatus.OPEN.value),
        )
        return int(cur.lastrowid or 0)

    async def list_tuning(
        self, *, borrower: str | None = None, status: TuningStatus | None = TuningStatus.OPEN,
        limit: int = 20,
    ) -> list[TuningRow]:
        sql = "SELECT * FROM tuning_suggestions WHERE 1=1"
        args: list[Any] = []
        if borrower is not None:
            sql += " AND borrower = ?"
            args.append(borrower)
        if status is not None:
            sql += " AND status = ?"
            args.append(status.value)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [self._tuning(r) for r in await self._rows(sql, tuple(args))]

    async def get_tuning(self, tuning_id: int) -> TuningRow | None:
        row = await self._row("SELECT * FROM tuning_suggestions WHERE id = ?", (tuning_id,))
        return None if row is None else self._tuning(row)

    async def set_tuning_status(
        self, tuning_id: int, status: TuningStatus, *, now: float | None = None
    ) -> bool:
        cur = await self._write(
            "UPDATE tuning_suggestions SET status = ?, decided_at = ? WHERE id = ? AND status = ?",
            (status.value, time.time() if now is None else now, tuning_id, TuningStatus.OPEN.value),
        )
        return cur.rowcount == 1

    @staticmethod
    def _tuning(row: sqlite3.Row) -> TuningRow:
        return TuningRow(
            id=row["id"], run_id=row["run_id"], borrower=row["borrower"],
            created_at=row["created_at"], field_name=row["field_name"],
            current_value=row["current_value"], suggested_value=row["suggested_value"],
            rationale=row["rationale"],
            eip712_payload=json.loads(row["eip712_payload_json"]),
            status=TuningStatus(row["status"]), decided_at=row["decided_at"],
        )

    # --- Decisions (per-node crew trace) ---------------------------------------------------

    async def insert_decision(
        self,
        *,
        run_id: str,
        borrower: str,
        node: str,
        output: dict[str, Any],
        latency_ms: int | None = None,
        llm_used: bool = False,
        error: str | None = None,
        now: float | None = None,
    ) -> int:
        cur = await self._write(
            "INSERT INTO decisions (run_id, borrower, created_at, node, latency_ms, llm_used,"
            " output_json, error) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, borrower, time.time() if now is None else now, node, latency_ms,
             int(llm_used), json.dumps(output), error),
        )
        return int(cur.lastrowid or 0)

    async def list_decisions(self, run_id: str) -> list[DecisionRow]:
        rows = await self._rows("SELECT * FROM decisions WHERE run_id = ? ORDER BY id", (run_id,))
        return [
            DecisionRow(
                id=r["id"], run_id=r["run_id"], borrower=r["borrower"],
                created_at=r["created_at"], node=r["node"], latency_ms=r["latency_ms"],
                llm_used=bool(r["llm_used"]), output=json.loads(r["output_json"]),
                error=r["error"],
            )
            for r in rows
        ]

    # --- Audit -----------------------------------------------------------------------------

    async def audit(
        self,
        *,
        actor: str,
        action: AuditAction,
        borrower: str | None = None,
        proposal_id: int | None = None,
        detail: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> int:
        cur = await self._write(
            "INSERT INTO audit (ts, actor, action, borrower, proposal_id, detail_json)"
            " VALUES (?,?,?,?,?,?)",
            (time.time() if now is None else now, actor, action.value, borrower, proposal_id,
             json.dumps(detail or {})),
        )
        return int(cur.lastrowid or 0)

    async def list_audit(
        self, *, borrower: str | None = None, limit: int = 50
    ) -> list[AuditRow]:
        if borrower is None:
            rows = await self._rows("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = await self._rows(
                "SELECT * FROM audit WHERE borrower = ? ORDER BY id DESC LIMIT ?",
                (borrower, limit),
            )
        return [
            AuditRow(
                id=r["id"], ts=r["ts"], actor=r["actor"], action=AuditAction(r["action"]),
                borrower=r["borrower"], proposal_id=r["proposal_id"],
                detail=json.loads(r["detail_json"]),
            )
            for r in rows
        ]

    # --- Chat ------------------------------------------------------------------------------

    async def ensure_thread(
        self, thread_id: str, *, borrower: str | None = None, now: float | None = None
    ) -> None:
        ts = time.time() if now is None else now
        await self._write(
            "INSERT INTO chat_threads (thread_id, created_at, last_active, borrower)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(thread_id) DO UPDATE SET last_active = excluded.last_active,"
            " borrower = COALESCE(excluded.borrower, chat_threads.borrower)",
            (thread_id, ts, ts, borrower),
        )

    async def append_message(
        self,
        *,
        thread_id: str,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        facts: dict[str, Any] | None = None,
        guard_flagged: bool = False,
        now: float | None = None,
    ) -> int:
        cur = await self._write(
            "INSERT INTO chat_messages (thread_id, ts, role, content, tool_calls_json,"
            " facts_json, guard_flagged) VALUES (?,?,?,?,?,?,?)",
            (thread_id, time.time() if now is None else now, role, content,
             json.dumps(tool_calls) if tool_calls is not None else None,
             json.dumps(facts) if facts is not None else None, int(guard_flagged)),
        )
        return int(cur.lastrowid or 0)

    async def history(self, thread_id: str, *, limit: int = 40) -> list[ChatMessageRow]:
        """The last ``limit`` messages of a thread, oldest first.

        Ordered DESC in SQL then reversed so the *newest* window is kept when a thread is long —
        selecting the oldest N would replay the wrong end of the conversation to the model.
        """
        rows = await self._rows(
            "SELECT * FROM chat_messages WHERE thread_id = ? ORDER BY id DESC LIMIT ?",
            (thread_id, limit),
        )
        return [
            ChatMessageRow(
                id=r["id"], thread_id=r["thread_id"], ts=r["ts"], role=r["role"],
                content=r["content"], tool_calls=_loads(r["tool_calls_json"]),
                facts=_loads(r["facts_json"]), guard_flagged=bool(r["guard_flagged"]),
            )
            for r in reversed(rows)
        ]

    async def delete_thread(self, thread_id: str) -> bool:
        # chat_messages cascades via the FK declared above (PRAGMA foreign_keys=ON).
        cur = await self._write("DELETE FROM chat_threads WHERE thread_id = ?", (thread_id,))
        return cur.rowcount == 1
