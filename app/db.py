from __future__ import annotations

import csv
import io
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL DEFAULT 'vk',
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    question TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    support_message_id INTEGER,
                    human_answer TEXT,
                    escalation_reason TEXT,
                    confidence REAL,
                    intake_type TEXT,
                    intake_payload_json TEXT,
                    intake_request_id TEXT,
                    priority TEXT NOT NULL DEFAULT 'normal',
                    knowledge_candidate_status TEXT,
                    knowledge_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    answered_at TEXT,
                    closed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS manual_faq (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'approved_manager_knowledge',
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    kind TEXT NOT NULL DEFAULT 'permanent',
                    expires_at TEXT,
                    created_by INTEGER,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS answer_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL DEFAULT 'vk',
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    question TEXT NOT NULL,
                    faq_id TEXT,
                    reason TEXT,
                    confidence REAL,
                    helpful INTEGER,
                    feedback_at TEXT,
                    escalated_ticket_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(escalated_ticket_id) REFERENCES tickets(id)
                );

                CREATE TABLE IF NOT EXISTS interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL DEFAULT 'vk',
                    user_id INTEGER NOT NULL,
                    peer_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    meta_json TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS conversation_context (
                    platform TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    topic TEXT,
                    entity TEXT,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(platform,user_id)
                );

                CREATE TABLE IF NOT EXISTS notification_subscriptions (
                    platform TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    peer_id INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    subscribed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(platform,user_id)
                );

                CREATE TABLE IF NOT EXISTS admin_state (
                    platform TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    state TEXT,
                    payload_json TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(platform,user_id)
                );

                CREATE TABLE IF NOT EXISTS newsletter_campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    target_count INTEGER NOT NULL DEFAULT 0,
                    sent_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    created_by INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    sent_at TEXT
                );

                CREATE TABLE IF NOT EXISTS booking_sessions (
                    platform TEXT NOT NULL DEFAULT 'vk',
                    user_id INTEGER NOT NULL,
                    peer_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    step TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    external_booking_id TEXT,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(platform,user_id)
                );

                CREATE TABLE IF NOT EXISTS support_intake_sessions (
                    platform TEXT NOT NULL DEFAULT 'vk',
                    user_id INTEGER NOT NULL,
                    peer_id INTEGER NOT NULL,
                    intake_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    step TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    original_question TEXT NOT NULL DEFAULT '',
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(platform,user_id)
                );

                CREATE TABLE IF NOT EXISTS shift_sessions (
                    platform TEXT NOT NULL DEFAULT 'vk',
                    user_id INTEGER NOT NULL, peer_id INTEGER NOT NULL,
                    step TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
                    expires_at TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(platform,user_id)
                );
                CREATE TABLE IF NOT EXISTS shift_applications (
                    application_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL, peer_id INTEGER NOT NULL,
                    shift_number INTEGER NOT NULL CHECK(shift_number BETWEEN 1 AND 5),
                    child_name TEXT NOT NULL, child_birth_date TEXT NOT NULL,
                    parent_name TEXT NOT NULL, phone TEXT NOT NULL,
                    consent_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new',
                    payment_status TEXT NOT NULL DEFAULT 'not_paid',
                    manager_id INTEGER,
                    sync_status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_shift_status ON shift_applications(status,created_at);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_manual_faq_unique ON manual_faq(question, answer);
                CREATE INDEX IF NOT EXISTS idx_ticket_support_message ON tickets(support_message_id);
                CREATE INDEX IF NOT EXISTS idx_ticket_status ON tickets(status);
                CREATE INDEX IF NOT EXISTS idx_ticket_platform ON tickets(platform);
                CREATE INDEX IF NOT EXISTS idx_answer_events_created ON answer_events(created_at);
                CREATE INDEX IF NOT EXISTS idx_answer_events_feedback ON answer_events(helpful);
                CREATE INDEX IF NOT EXISTS idx_answer_events_platform ON answer_events(platform);
                CREATE INDEX IF NOT EXISTS idx_interactions_created ON interactions(created_at);
                CREATE INDEX IF NOT EXISTS idx_interactions_kind ON interactions(kind,name);
                CREATE INDEX IF NOT EXISTS idx_subscriptions_enabled ON notification_subscriptions(platform,enabled);
                CREATE INDEX IF NOT EXISTS idx_booking_sessions_external ON booking_sessions(external_booking_id);
                CREATE INDEX IF NOT EXISTS idx_intake_sessions_type ON support_intake_sessions(intake_type,status);
                """
            )

            # Additive migrations from v5.5 and older. Never delete user data.
            ticket_columns = {row[1] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()}
            for name, sql_type, default_sql in [
                ("escalation_reason", "TEXT", None),
                ("confidence", "REAL", None),
                ("answered_at", "TEXT", None),
                ("platform", "TEXT", "'vk'"),
                ("intake_type", "TEXT", None),
                ("intake_payload_json", "TEXT", None),
                ("intake_request_id", "TEXT", None),
                ("priority", "TEXT", "'normal'"),
                ("knowledge_candidate_status", "TEXT", None),
                ("knowledge_id", "INTEGER", None),
            ]:
                if name not in ticket_columns:
                    suffix = f" DEFAULT {default_sql}" if default_sql is not None else ""
                    conn.execute(f"ALTER TABLE tickets ADD COLUMN {name} {sql_type}{suffix}")
            # Create the v6 idempotency index only after additive migration has added the column
            # to an existing v5.9 database.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_ticket_intake_request "
                "ON tickets(intake_request_id) WHERE intake_request_id IS NOT NULL"
            )
            answer_columns = {row[1] for row in conn.execute("PRAGMA table_info(answer_events)").fetchall()}
            if "platform" not in answer_columns:
                conn.execute("ALTER TABLE answer_events ADD COLUMN platform TEXT DEFAULT 'vk'")

            # v5.7 additive knowledge-base migration. Existing approved answers are preserved.
            faq_columns = {row[1] for row in conn.execute("PRAGMA table_info(manual_faq)").fetchall()}
            for name, sql_type, default_sql in [
                ("aliases_json", "TEXT", "'[]'"),
                ("kind", "TEXT", "'permanent'"),
                ("expires_at", "TEXT", None),
                ("created_by", "INTEGER", None),
                ("active", "INTEGER", "1"),
            ]:
                if name not in faq_columns:
                    suffix = f" DEFAULT {default_sql}" if default_sql is not None else ""
                    conn.execute(f"ALTER TABLE manual_faq ADD COLUMN {name} {sql_type}{suffix}")
            # Normalize the source label of legacy operator-approved FAQ without losing any rows.
            conn.execute(
                "UPDATE manual_faq SET source='approved_manager_knowledge' "
                "WHERE source IN ('approved_operator_answer','approved_manager_answer')"
            )
            # A persisted `submitting` state can only survive if the previous process stopped
            # between the parent's confirmation and cleanup. On a fresh process there is no
            # in-flight coroutine, so make the form retryable. Ticket idempotency prevents duplicates.
            conn.execute(
                "UPDATE support_intake_sessions SET status='confirm_parent',updated_at=CURRENT_TIMESTAMP "
                "WHERE status='submitting'"
            )

    # ---------------- tickets / FAQ ----------------
    def create_ticket(self, *, user_id: int, chat_id: int, username: str | None, full_name: str,
                      question: str, reason: str = "", confidence: float = 0.0,
                      platform: str = "vk", intake_type: str | None = None,
                      intake_payload: dict[str, Any] | None = None, priority: str = "normal",
                      intake_request_id: str | None = None) -> int:
        priority = priority if priority in {"low", "normal", "high", "urgent"} else "normal"
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO tickets(platform,user_id,chat_id,username,full_name,question,escalation_reason,confidence,
                                       intake_type,intake_payload_json,intake_request_id,priority)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (platform, user_id, chat_id, username, full_name, question, reason, confidence,
                 intake_type, json.dumps(intake_payload or {}, ensure_ascii=False) if intake_type else None,
                 intake_request_id, priority),
            )
            return int(cur.lastrowid)

    def get_ticket_by_intake_request(self, request_id: str) -> sqlite3.Row | None:
        request_id = (request_id or "").strip()
        if not request_id:
            return None
        with self._conn() as conn:
            return conn.execute("SELECT * FROM tickets WHERE intake_request_id=?", (request_id,)).fetchone()

    def reopen_failed_ticket(self, ticket_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE tickets SET status='open',closed_at=NULL WHERE id=? AND status='failed'",
                (int(ticket_id),),
            )

    def set_support_message(self, ticket_id: int, message_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE tickets SET support_message_id=? WHERE id=?", (message_id, ticket_id))

    def get_ticket(self, ticket_id: int) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()

    def get_ticket_by_support_message(self, message_id: int) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM tickets WHERE support_message_id=?", (message_id,)).fetchone()

    def list_open_tickets(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM tickets WHERE status='open' ORDER BY id DESC LIMIT ?", (max(1, min(limit, 50)),)
            ).fetchall()

    def save_human_answer(self, ticket_id: int, answer: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE tickets SET human_answer=?,status='answered',answered_at=CURRENT_TIMESTAMP WHERE id=?",
                (answer, ticket_id),
            )

    def mark_failed(self, ticket_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE tickets SET status='failed',closed_at=CURRENT_TIMESTAMP WHERE id=?", (ticket_id,))

    def close_ticket(self, ticket_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE tickets SET status='closed',closed_at=CURRENT_TIMESTAMP WHERE id=?", (ticket_id,))

    def set_ticket_knowledge_candidate(self, ticket_id: int, status: str, knowledge_id: int | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE tickets SET knowledge_candidate_status=?, knowledge_id=? WHERE id=?",
                (status, knowledge_id, int(ticket_id)),
            )

    def add_manual_faq(self, question: str, answer: str) -> bool:
        """Backward-compatible helper used by older tests/imports.

        v5.7 treats these as permanent manager-approved knowledge.
        """
        return bool(self.add_manual_knowledge(question=question, answer=answer))

    def add_manual_knowledge(
        self, *, question: str, answer: str, aliases: list[str] | None = None,
        kind: str = "permanent", expires_at: datetime | str | None = None, created_by: int | None = None,
    ) -> int | None:
        question, answer = question.strip(), answer.strip()
        if not question or not answer:
            return None
        aliases = [str(x).strip() for x in (aliases or []) if str(x).strip()]
        # Preserve order while removing duplicates / copies of the main question.
        seen = {question.lower().replace("ё", "е")}
        clean_aliases: list[str] = []
        for alias in aliases:
            key = alias.lower().replace("ё", "е")
            if key in seen:
                continue
            seen.add(key); clean_aliases.append(alias)
        if isinstance(expires_at, datetime):
            expires = expires_at.astimezone(timezone.utc).isoformat() if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc).isoformat()
        elif expires_at:
            expires = str(expires_at)
        else:
            expires = None
        kind = "temporary" if kind == "temporary" else "permanent"
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO manual_faq(question,answer,source,aliases_json,kind,expires_at,created_by,active)
                   VALUES(?,?,'approved_manager_knowledge',?,?,?,?,1)""",
                (question, answer, json.dumps(clean_aliases, ensure_ascii=False), kind, expires, created_by),
            )
            if cur.rowcount <= 0:
                return None
            return int(cur.lastrowid)

    @staticmethod
    def _manual_row_to_faq(row: sqlite3.Row) -> dict[str, Any]:
        try:
            aliases = json.loads(str(row["aliases_json"] or "[]"))
            if not isinstance(aliases, list): aliases = []
        except (json.JSONDecodeError, TypeError):
            aliases = []
        return {
            "id": f"manual-{int(row['id'])}",
            "manual_id": int(row["id"]),
            "question": str(row["question"]),
            "answer": str(row["answer"]),
            "source": str(row["source"] or "approved_manager_knowledge"),
            "aliases": [str(x) for x in aliases if str(x).strip()],
            "kind": str(row["kind"] or "permanent"),
            "expires_at": str(row["expires_at"] or "") or None,
            "created_at": str(row["created_at"] or ""),
            "active": bool(row["active"]) if "active" in row.keys() else True,
        }

    def list_manual_faq(self) -> list[dict[str, Any]]:
        """Only active, non-expired knowledge participates in automatic answers."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id,question,answer,source,aliases_json,kind,expires_at,active,created_at
                   FROM manual_faq
                   WHERE active=1 AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))
                   ORDER BY id DESC"""
            ).fetchall()
        return [self._manual_row_to_faq(r) for r in rows]

    def list_manual_knowledge(self, limit: int = 20, *, include_expired: bool = True) -> list[dict[str, Any]]:
        where = "" if include_expired else "WHERE active=1 AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))"
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT id,question,answer,source,aliases_json,kind,expires_at,active,created_at
                    FROM manual_faq {where} ORDER BY id DESC LIMIT ?""",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [self._manual_row_to_faq(r) for r in rows]

    def delete_manual_knowledge(self, knowledge_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("UPDATE manual_faq SET active=0 WHERE id=? AND active=1", (int(knowledge_id),))
            return cur.rowcount > 0

    def manual_knowledge_count(self, *, active_only: bool = True) -> int:
        where = "WHERE active=1 AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))" if active_only else ""
        with self._conn() as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM manual_faq {where}").fetchone()[0])

    # ---------------- answers / feedback ----------------
    def log_answer(self, *, user_id: int, chat_id: int, username: str | None, full_name: str,
                   question: str, faq_id: str | None, reason: str, confidence: float,
                   platform: str = "vk") -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO answer_events(platform,user_id,chat_id,username,full_name,question,faq_id,reason,confidence)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (platform, user_id, chat_id, username, full_name, question, faq_id, reason, confidence),
            )
            return int(cur.lastrowid)

    def get_answer_event(self, event_id: int) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM answer_events WHERE id=?", (event_id,)).fetchone()

    def set_feedback(self, event_id: int, helpful: bool) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE answer_events SET helpful=?,feedback_at=CURRENT_TIMESTAMP WHERE id=?", (1 if helpful else 0, event_id))

    def link_feedback_ticket(self, event_id: int, ticket_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE answer_events SET escalated_ticket_id=? WHERE id=?", (ticket_id, event_id))

    def feedback_stats(self, platform: str | None = None, days: int | None = None) -> dict[str, int | float]:
        clauses, params = [], []
        if platform:
            clauses.append("platform=?"); params.append(platform)
        if days:
            clauses.append("created_at >= datetime('now', ?)"); params.append(f"-{int(days)} days")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM answer_events{where}", tuple(params)).fetchone()[0])
            def count(extra: str) -> int:
                prefix = " WHERE " if not clauses else " AND "
                return int(conn.execute(f"SELECT COUNT(*) FROM answer_events{where}{prefix}{extra}", tuple(params)).fetchone()[0])
            rated = count("helpful IS NOT NULL")
            helpful = count("helpful=1")
            not_helpful = count("helpful=0")
        return {
            "answers": total,
            "rated": rated,
            "helpful": helpful,
            "not_helpful": not_helpful,
            "helpful_rate": round(helpful / rated * 100, 1) if rated else 0.0,
        }

    def export_feedback_csv(self) -> bytes:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id,created_at,platform,user_id,username,full_name,question,faq_id,reason,confidence,
                          helpful,feedback_at,escalated_ticket_id FROM answer_events ORDER BY id DESC"""
            ).fetchall()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id","created_at","platform","user_id","username","full_name","question","faq_id",
                         "reason","confidence","helpful","feedback_at","escalated_ticket_id"])
        for row in rows:
            writer.writerow([row[k] for k in row.keys()])
        return buf.getvalue().encode("utf-8-sig")

    # ---------------- interaction analytics ----------------
    def track(self, *, user_id: int, peer_id: int, kind: str, name: str = "",
              meta: dict[str, Any] | None = None, platform: str = "vk") -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO interactions(platform,user_id,peer_id,kind,name,meta_json) VALUES(?,?,?,?,?,?)",
                (platform, user_id, peer_id, kind, name, json.dumps(meta, ensure_ascii=False) if meta else None),
            )
            return int(cur.lastrowid)

    def has_seen_user(self, *, user_id: int, platform: str = "vk") -> bool:
        """Whether this user has any prior interaction recorded before the current message."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM interactions WHERE platform=? AND user_id=? LIMIT 1",
                (platform, user_id),
            ).fetchone()
        return row is not None

    # ---------------- booking sessions ----------------
    def save_booking_session(self, *, user_id: int, peer_id: int, step: str, status: str,
                             payload: dict[str, Any], external_booking_id: str | None = None,
                             ttl_minutes: int = 60, platform: str = "vk") -> None:
        expires = datetime.now(timezone.utc) + timedelta(minutes=max(5, ttl_minutes))
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO booking_sessions(platform,user_id,peer_id,status,step,payload_json,external_booking_id,expires_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(platform,user_id) DO UPDATE SET peer_id=excluded.peer_id,status=excluded.status,
                     step=excluded.step,payload_json=excluded.payload_json,external_booking_id=excluded.external_booking_id,
                     expires_at=excluded.expires_at,updated_at=CURRENT_TIMESTAMP""",
                (platform, user_id, peer_id, status, step, json.dumps(payload, ensure_ascii=False), external_booking_id, expires.isoformat()),
            )

    def get_booking_session(self, *, user_id: int, platform: str = "vk") -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT peer_id,status,step,payload_json,external_booking_id,expires_at FROM booking_sessions WHERE platform=? AND user_id=?",
                (platform, user_id),
            ).fetchone()
        if not row:
            return None
        try:
            expires = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires < datetime.now(timezone.utc):
                self.clear_booking_session(user_id=user_id, platform=platform)
                return None
        except ValueError:
            self.clear_booking_session(user_id=user_id, platform=platform)
            return None
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
            if not isinstance(payload, dict):
                payload = {}
        except json.JSONDecodeError:
            payload = {}
        return {
            "peer_id": int(row["peer_id"]), "status": str(row["status"] or ""),
            "step": str(row["step"] or ""), "payload": payload,
            "external_booking_id": str(row["external_booking_id"] or ""),
            "expires_at": str(row["expires_at"] or ""),
        }

    def clear_booking_session(self, *, user_id: int, platform: str = "vk") -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM booking_sessions WHERE platform=? AND user_id=?", (platform, user_id))

    def find_booking_session_by_external_id(self, booking_id: str, *, platform: str = "vk") -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT user_id,peer_id,status,step,payload_json,expires_at FROM booking_sessions WHERE platform=? AND external_booking_id=?",
                (platform, booking_id),
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except json.JSONDecodeError:
            payload = {}
        return {"user_id": int(row["user_id"]), "peer_id": int(row["peer_id"]), "status": str(row["status"] or ""), "step": str(row["step"] or ""), "payload": payload}

    def analytics_stats(self, *, days: int | None = 30, platform: str = "vk") -> dict[str, Any]:
        p: list[Any] = [platform]
        where = "platform=?"
        ticket_where = "platform=?"
        answer_where = "platform=?"
        if days:
            where += " AND created_at >= datetime('now', ?)"; p.append(f"-{int(days)} days")
            ticket_where += " AND created_at >= datetime('now', ?)"
            answer_where += " AND created_at >= datetime('now', ?)"
        with self._conn() as conn:
            users = int(conn.execute(f"SELECT COUNT(DISTINCT user_id) FROM interactions WHERE {where}", tuple(p)).fetchone()[0])
            messages = int(conn.execute(f"SELECT COUNT(*) FROM interactions WHERE {where} AND kind='message'", tuple(p)).fetchone()[0])
            buttons = int(conn.execute(f"SELECT COUNT(*) FROM interactions WHERE {where} AND kind='button'", tuple(p)).fetchone()[0])
            starts = int(conn.execute(f"SELECT COUNT(*) FROM interactions WHERE {where} AND kind='start'", tuple(p)).fetchone()[0])
            ep = [platform] + ([f"-{int(days)} days"] if days else [])
            auto = int(conn.execute(f"SELECT COUNT(*) FROM answer_events WHERE {answer_where}", tuple(ep)).fetchone()[0])
            escalated = int(conn.execute(f"SELECT COUNT(*) FROM tickets WHERE {ticket_where}", tuple(ep)).fetchone()[0])
            open_tickets = int(conn.execute(f"SELECT COUNT(*) FROM tickets WHERE {ticket_where} AND status='open'", tuple(ep)).fetchone()[0])
            subs = int(conn.execute("SELECT COUNT(*) FROM notification_subscriptions WHERE platform=? AND enabled=1", (platform,)).fetchone()[0])
            top_buttons = conn.execute(
                f"SELECT name,COUNT(*) c FROM interactions WHERE {where} AND kind='button' GROUP BY name ORDER BY c DESC LIMIT 6",
                tuple(p),
            ).fetchall()
            bad_faq = conn.execute(
                f"SELECT COALESCE(faq_id,'(без FAQ)') faq,COUNT(*) c FROM answer_events WHERE {answer_where} AND helpful=0 GROUP BY faq ORDER BY c DESC LIMIT 6",
                tuple(ep),
            ).fetchall()
        feedback = self.feedback_stats(platform, days)
        handled = auto + escalated
        containment = round(auto / handled * 100, 1) if handled else 0.0
        return {
            "users": users, "messages": messages, "buttons": buttons, "starts": starts,
            "auto_answers": auto, "escalated": escalated, "open": open_tickets,
            "subscribers": subs, "containment_rate": containment,
            **feedback,
            "top_buttons": [(str(r["name"]), int(r["c"])) for r in top_buttons],
            "bad_faq": [(str(r["faq"]), int(r["c"])) for r in bad_faq],
        }

    # ---------------- Smart Handoff intake sessions ----------------
    def save_intake_session(
        self, *, user_id: int, peer_id: int, intake_type: str, step: str, status: str,
        payload: dict[str, Any], ttl_minutes: int = 90, platform: str = "vk",
        original_question: str = "",
    ) -> None:
        expires = datetime.now(timezone.utc) + timedelta(minutes=max(1, int(ttl_minutes)))
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO support_intake_sessions(
                       platform,user_id,peer_id,intake_type,status,step,payload_json,original_question,expires_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(platform,user_id) DO UPDATE SET
                     peer_id=excluded.peer_id,intake_type=excluded.intake_type,status=excluded.status,
                     step=excluded.step,payload_json=excluded.payload_json,original_question=excluded.original_question,
                     expires_at=excluded.expires_at,updated_at=CURRENT_TIMESTAMP""",
                (platform, int(user_id), int(peer_id), intake_type, status, step,
                 json.dumps(payload or {}, ensure_ascii=False), original_question, expires.isoformat()),
            )

    @staticmethod
    def _intake_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
            if not isinstance(payload, dict):
                payload = {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
        return {
            "peer_id": int(row["peer_id"]),
            "intake_type": str(row["intake_type"]),
            "status": str(row["status"]),
            "step": str(row["step"]),
            "payload": payload,
            "original_question": str(row["original_question"] or ""),
            "expires_at": str(row["expires_at"]),
            "updated_at": str(row["updated_at"] or ""),
        }

    def get_intake_session(self, *, user_id: int, platform: str = "vk") -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM support_intake_sessions WHERE platform=? AND user_id=?", (platform, int(user_id))
            ).fetchone()
            if row is None:
                return None
            try:
                expires = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
            except ValueError:
                expires = now - timedelta(seconds=1)
            if expires.astimezone(timezone.utc) <= now:
                conn.execute("DELETE FROM support_intake_sessions WHERE platform=? AND user_id=?", (platform, int(user_id)))
                return None
            return self._intake_row(row)

    def clear_intake_session(self, *, user_id: int, platform: str = "vk") -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM support_intake_sessions WHERE platform=? AND user_id=?", (platform, int(user_id)))

    def claim_intake_submission(self, *, user_id: int, platform: str = "vk") -> dict[str, Any] | None:
        """Atomically move confirm_parent -> submitting to prevent duplicate callback tickets."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM support_intake_sessions WHERE platform=? AND user_id=?", (platform, int(user_id))
            ).fetchone()
            if row is None or str(row["status"] or "") != "confirm_parent":
                return None
            cur = conn.execute(
                """UPDATE support_intake_sessions SET status='submitting',updated_at=CURRENT_TIMESTAMP
                   WHERE platform=? AND user_id=? AND status='confirm_parent'""",
                (platform, int(user_id)),
            )
            if cur.rowcount != 1:
                return None
            fresh = conn.execute(
                "SELECT * FROM support_intake_sessions WHERE platform=? AND user_id=?", (platform, int(user_id))
            ).fetchone()
            return self._intake_row(fresh)

    def release_intake_submission(self, *, user_id: int, platform: str = "vk") -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE support_intake_sessions
                   SET status='confirm_parent',step='confirm_parent',updated_at=CURRENT_TIMESTAMP
                   WHERE platform=? AND user_id=? AND status='submitting'""",
                (platform, int(user_id)),
            )

    def intake_session_count(self, *, active_only: bool = True) -> int:
        where = "WHERE datetime(expires_at) > datetime('now')" if active_only else ""
        with self._conn() as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM support_intake_sessions {where}").fetchone()[0])

    # ---------------- short context ----------------
    def set_context(self, *, user_id: int, topic: str | None, entity: str | None,
                    ttl_minutes: int = 15, platform: str = "vk") -> None:
        expires = datetime.now(timezone.utc) + timedelta(minutes=max(1, ttl_minutes))
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO conversation_context(platform,user_id,topic,entity,expires_at,updated_at)
                   VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(platform,user_id) DO UPDATE SET topic=excluded.topic,entity=excluded.entity,
                     expires_at=excluded.expires_at,updated_at=CURRENT_TIMESTAMP""",
                (platform, user_id, topic, entity, expires.isoformat()),
            )

    def get_context(self, *, user_id: int, platform: str = "vk") -> dict[str, str] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT topic,entity,expires_at FROM conversation_context WHERE platform=? AND user_id=?", (platform, user_id)).fetchone()
        if not row:
            return None
        try:
            expires = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires < datetime.now(timezone.utc):
                self.clear_context(user_id=user_id, platform=platform)
                return None
        except ValueError:
            return None
        return {"topic": str(row["topic"] or ""), "entity": str(row["entity"] or "")}

    def clear_context(self, *, user_id: int, platform: str = "vk") -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM conversation_context WHERE platform=? AND user_id=?", (platform, user_id))

    # ---------------- subscriptions / newsletters ----------------
    def set_subscription(self, *, user_id: int, peer_id: int, enabled: bool, platform: str = "vk") -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO notification_subscriptions(platform,user_id,peer_id,enabled,updated_at)
                   VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(platform,user_id) DO UPDATE SET peer_id=excluded.peer_id,enabled=excluded.enabled,
                     updated_at=CURRENT_TIMESTAMP""",
                (platform, user_id, peer_id, 1 if enabled else 0),
            )

    def is_subscribed(self, *, user_id: int, platform: str = "vk") -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT enabled FROM notification_subscriptions WHERE platform=? AND user_id=?", (platform, user_id)).fetchone()
        return bool(row and int(row["enabled"]) == 1)

    def subscriber_peer_ids(self, *, platform: str = "vk") -> list[int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT peer_id FROM notification_subscriptions WHERE platform=? AND enabled=1 ORDER BY user_id", (platform,)).fetchall()
        return [int(r["peer_id"]) for r in rows]

    def create_campaign(self, *, text: str, created_by: int, target_count: int) -> int:
        with self._conn() as conn:
            cur = conn.execute("INSERT INTO newsletter_campaigns(text,created_by,target_count) VALUES(?,?,?)", (text, created_by, target_count))
            return int(cur.lastrowid)

    def get_campaign(self, campaign_id: int) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM newsletter_campaigns WHERE id=?", (campaign_id,)).fetchone()

    def finish_campaign(self, campaign_id: int, *, sent: int, failed: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE newsletter_campaigns SET status='sent',sent_count=?,failed_count=?,sent_at=CURRENT_TIMESTAMP WHERE id=?",
                (sent, failed, campaign_id),
            )

    # ---------------- admin state ----------------
    def set_admin_state(self, *, user_id: int, state: str | None, payload: dict[str, Any] | None = None,
                        platform: str = "vk") -> None:
        with self._conn() as conn:
            if not state:
                conn.execute("DELETE FROM admin_state WHERE platform=? AND user_id=?", (platform, user_id))
                return
            conn.execute(
                """INSERT INTO admin_state(platform,user_id,state,payload_json,updated_at)
                   VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(platform,user_id) DO UPDATE SET state=excluded.state,payload_json=excluded.payload_json,
                     updated_at=CURRENT_TIMESTAMP""",
                (platform, user_id, state, json.dumps(payload, ensure_ascii=False) if payload else None),
            )

    def get_admin_state(self, *, user_id: int, platform: str = "vk") -> tuple[str, dict[str, Any]] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT state,payload_json FROM admin_state WHERE platform=? AND user_id=?", (platform, user_id)).fetchone()
        if not row:
            return None
        payload: dict[str, Any] = {}
        if row["payload_json"]:
            try: payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError: payload = {}
        return str(row["state"] or ""), payload

    def stats(self) -> dict[str, Any]:
        s = self.analytics_stats(days=None)
        with self._conn() as conn:
            faq_count = self.manual_knowledge_count(active_only=True)
            failed = int(conn.execute("SELECT COUNT(*) FROM tickets WHERE status='failed'").fetchone()[0])
            closed = int(conn.execute("SELECT COUNT(*) FROM tickets WHERE status='closed'").fetchone()[0])
            answered = int(conn.execute("SELECT COUNT(*) FROM tickets WHERE status='answered'").fetchone()[0])
            total = int(conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0])
        s.update({"faq": faq_count, "failed": failed, "closed": closed, "answered": answered, "total": total})
        return s
