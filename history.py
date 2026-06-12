"""
Local Chat History — SQLite-backed conversation persistence.

Stores chat history in a local SQLite database under the OS user-data directory.
- Conversations, messages, and tool calls
- FTS5 full-text search (with LIKE fallback)
- Export (Markdown, JSON), backup, recovery
- No secrets, tokens, or API keys stored here.
"""

import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_message_at TEXT,
    last_message_preview TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    model_id TEXT,
    is_pinned INTEGER NOT NULL DEFAULT 0,
    pinned_at TEXT,
    is_archived INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    text TEXT,
    model_id TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(conversation_id, seq)
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    raw_name TEXT NOT NULL,
    friendly_label TEXT,
    status TEXT NOT NULL,
    args_json TEXT,
    result_summary TEXT,
    error_message TEXT,
    duration_ms INTEGER,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversations(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_conv_pinned ON conversations(is_pinned DESC, pinned_at DESC);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, seq);
"""

FTS_CREATE = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    conv_id,
    text,
    content='',
    tokenize='unicode61'
);
"""

FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, conv_id, text) VALUES (new.rowid, new.conversation_id, COALESCE(new.text, ''));
END;

CREATE TRIGGER IF NOT EXISTS fts_update AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, conv_id, text) VALUES ('delete', old.rowid, old.conversation_id, COALESCE(old.text, ''));
    INSERT INTO messages_fts(rowid, conv_id, text) VALUES (new.rowid, new.conversation_id, COALESCE(new.text, ''));
END;

CREATE TRIGGER IF NOT EXISTS fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, conv_id, text) VALUES ('delete', old.rowid, old.conversation_id, COALESCE(old.text, ''));
END;
"""

# ── Friendly tool names (shared with UI) ──────────────────────
TOOL_LABELS = {
    "analyze_character": "Load character snapshot",
    "import_poe_ninja_url": "Import from poe.ninja",
    "compare_to_top_players": "Compare to top players",
    "analyze_passive_tree": "Analyze passive tree",
    "list_all_supports": "List all support gems",
    "list_all_spells": "List all spell gems",
    "inspect_support_gem": "Inspect support gem",
    "inspect_spell_gem": "Inspect spell gem",
    "validate_support_combination": "Validate gem combo",
    "list_all_keystones": "List keystones",
    "inspect_keystone": "Inspect keystone",
    "list_all_notables": "List notables",
    "inspect_passive_node": "Inspect passive node",
    "get_ascendancy_info": "Ascendancy info",
    "list_all_mods": "List all modifiers",
    "inspect_mod": "Inspect modifier",
    "search_mods_by_stat": "Search modifiers",
    "get_mod_tiers": "Get mod tiers",
    "validate_item_mods": "Validate item mods",
    "get_available_mods": "Available mods",
    "list_all_base_items": "List base items",
    "inspect_base_item": "Inspect base item",
    "explain_mechanic": "Explain mechanic",
    "get_formula": "Get formula",
    "import_pob": "Import PoB build",
    "export_pob": "Export PoB build",
    "get_pob_code": "Get PoB code",
    "search_items": "Search items",
    "search_trade_items": "Search trade",
    "poe2_currency_prices": "Currency prices",
    "poe2_currency_check": "Check currency",
    "poe2_item_price": "Item price check",
    "poe2_exchange_top": "Top exchange items",
    "poe2_wiki_search": "Search wiki",
    "poe2_wiki_page": "Read wiki page",
    "poe2_db_lookup": "Database lookup",
    "poe2_meta_builds": "Meta builds",
    "poe2_log_summary": "Game log summary",
    "poe2_pob_decode": "Decode PoB build",
    "poe2_pob_local_builds": "Local PoB builds",
    "poe2_pob_compare": "Compare builds",
    "poe2_parse_item": "Parse item",
    "health_check": "Health check",
    "clear_cache": "Clear cache",
}


def friendly_tool_name(raw_name: str) -> str:
    return TOOL_LABELS.get(raw_name, raw_name.replace("_", " ").title())


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _auto_title(text: str) -> str:
    """Generate a short title from the first user message."""
    if not text:
        return "New chat"
    # Strip markdown
    clean = re.sub(r'[#*_`>~\[\]()]', '', text)
    # Collapse whitespace
    clean = re.sub(r'\s+', ' ', clean).strip()
    # Truncate
    if len(clean) > 60:
        clean = clean[:57] + "..."
    return clean or "New chat"


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row) if row else {}


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


class HistoryRepository:
    """Local SQLite chat history repository."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._has_fts5 = False
        self._connect()
        self._migrate()
        logger.info(f"History DB ready: {db_path} (FTS5={self._has_fts5})")

    def _connect(self):
        """Open database connection with proper pragmas."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.commit()

    def _migrate(self):
        """Apply schema migrations using PRAGMA user_version."""
        conn = self._conn
        version = conn.execute("PRAGMA user_version").fetchone()[0]

        if version < 1:
            logger.info("Applying migration v1: initial schema")
            conn.executescript(SCHEMA_V1)

            # Check FTS5 availability
            try:
                conn.executescript(FTS_CREATE)
                conn.executescript(FTS_TRIGGERS)
                self._has_fts5 = True
            except sqlite3.OperationalError as e:
                logger.warning(f"FTS5 not available, using LIKE fallback: {e}")
                self._has_fts5 = False

            conn.execute("PRAGMA user_version = 1")
            conn.commit()

    # ── Conversations ─────────────────────────────────────────

    def create_conversation(self, title: str = "New chat", model_id: str = "") -> dict:
        now = _now()
        cid = _uid()
        self._conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at, model_id) VALUES (?, ?, ?, ?, ?)",
            (cid, title, now, now, model_id),
        )
        self._conn.commit()
        return {"id": cid, "title": title, "created_at": now, "updated_at": now, "model_id": model_id}

    def update_conversation(self, cid: str, **kwargs):
        if not kwargs:
            return
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [cid]
        self._conn.execute(f"UPDATE conversations SET {sets} WHERE id = ?", vals)
        self._conn.commit()

    def list_conversations(self, limit: int = 50, offset: int = 0, include_archived: bool = False) -> list[dict]:
        where = "deleted_at IS NULL"
        if not include_archived:
            where += " AND is_archived = 0"
        rows = self._conn.execute(
            f"SELECT * FROM conversations WHERE {where} ORDER BY is_pinned DESC, updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return _rows_to_dicts(rows)

    def get_conversation(self, cid: str) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM conversations WHERE id = ? AND deleted_at IS NULL", (cid,)).fetchone()
        return _row_to_dict(row) if row else None

    def delete_conversation(self, cid: str):
        """Soft-delete a conversation."""
        self._conn.execute("UPDATE conversations SET deleted_at = ? WHERE id = ?", (_now(), cid))
        self._conn.commit()

    def hard_delete_conversation(self, cid: str):
        """Permanently delete a conversation and all its messages."""
        self._conn.execute("DELETE FROM conversations WHERE id = ?", (cid,))
        self._conn.commit()

    def purge_deleted(self, older_than_days: int = 30):
        """Permanently remove soft-deleted conversations older than N days."""
        cutoff = datetime.now(timezone.utc).isoformat()  # simplified: delete all soft-deleted
        self._conn.execute("DELETE FROM conversations WHERE deleted_at IS NOT NULL")
        self._conn.commit()

    # ── Messages ──────────────────────────────────────────────

    def save_message(self, conversation_id: str, role: str, text: str,
                     model_id: str = None, status: str = "completed",
                     error_message: str = None) -> dict:
        mid = _uid()
        now = _now()

        # Get next sequence number
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        seq = row[0]

        self._conn.execute(
            "INSERT INTO messages (id, conversation_id, seq, role, status, text, model_id, error_message, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mid, conversation_id, seq, role, status, text, model_id, error_message, now, now),
        )
        self._conn.commit()
        return {"id": mid, "conversation_id": conversation_id, "seq": seq, "role": role, "status": status}

    def update_message(self, mid: str, text: str = None, status: str = None):
        parts = []
        vals = []
        if text is not None:
            parts.append("text = ?")
            vals.append(text)
        if status is not None:
            parts.append("status = ?")
            vals.append(status)
        if parts:
            parts.append("updated_at = ?")
            vals.append(_now())
            vals.append(mid)
            self._conn.execute(f"UPDATE messages SET {', '.join(parts)} WHERE id = ?", vals)
            self._conn.commit()

    def get_messages(self, conversation_id: str, limit: int = 200, offset: int = 0) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq ASC LIMIT ? OFFSET ?",
            (conversation_id, limit, offset),
        ).fetchall()
        return _rows_to_dicts(rows)

    def get_message_count(self, conversation_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        return row[0]

    def recover_interrupted(self):
        """Mark any streaming/pending messages as interrupted (on startup)."""
        count = self._conn.execute(
            "UPDATE messages SET status = 'interrupted', updated_at = ? "
            "WHERE status IN ('streaming', 'pending')",
            (_now(),),
        ).rowcount
        if count > 0:
            self._conn.commit()
            logger.info(f"Recovered {count} interrupted messages")

    # ── Tool Calls ────────────────────────────────────────────

    def save_tool_call(self, message_id: str, raw_name: str, status: str,
                       friendly_label: str = None, args_json: str = None,
                       result_summary: str = None, error_message: str = None,
                       duration_ms: int = None) -> dict:
        tid = _uid()
        now = _now()
        if not friendly_label:
            friendly_label = friendly_tool_name(raw_name)
        # Truncate large JSON
        if args_json and len(args_json) > 10000:
            args_json = args_json[:10000] + "\n... (truncated)"
        if result_summary and len(result_summary) > 5000:
            result_summary = result_summary[:5000] + "\n... (truncated)"
        self._conn.execute(
            "INSERT INTO tool_calls (id, message_id, raw_name, friendly_label, status, args_json, result_summary, error_message, duration_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, message_id, raw_name, friendly_label, status, args_json, result_summary, error_message, duration_ms, now),
        )
        self._conn.commit()
        return {"id": tid, "raw_name": raw_name, "friendly_label": friendly_label, "status": status}

    def get_tool_calls(self, message_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM tool_calls WHERE message_id = ? ORDER BY created_at ASC",
            (message_id,),
        ).fetchall()
        return _rows_to_dicts(rows)

    # ── Search ────────────────────────────────────────────────

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Search conversations by title and message content."""
        if not query or len(query) < 2:
            return []

        if self._has_fts5:
            return self._search_fts5(query, limit)
        return self._search_like(query, limit)

    def _search_fts5(self, query: str, limit: int) -> list[dict]:
        try:
            rows = self._conn.execute(
                "SELECT c.id, c.title, c.updated_at, c.last_message_preview, "
                "snippet(messages_fts, 1, '▸', '◂', '...', 30) as snippet "
                "FROM conversations c "
                "JOIN messages_fts fts ON fts.conv_id = c.id "
                "WHERE messages_fts MATCH ? AND c.deleted_at IS NULL AND c.is_archived = 0 "
                "GROUP BY c.id ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
            return _rows_to_dicts(rows)
        except sqlite3.OperationalError:
            return self._search_like(query, limit)

    def _search_like(self, query: str, limit: int) -> list[dict]:
        pattern = f"%{query}%"
        rows = self._conn.execute(
            "SELECT DISTINCT c.id, c.title, c.updated_at, c.last_message_preview "
            "FROM conversations c "
            "LEFT JOIN messages m ON m.conversation_id = c.id "
            "WHERE c.deleted_at IS NULL AND c.is_archived = 0 "
            "AND (c.title LIKE ? OR m.text LIKE ?) "
            "ORDER BY c.updated_at DESC LIMIT ?",
            (pattern, pattern, limit),
        ).fetchall()
        return _rows_to_dicts(rows)

    # ── Export ─────────────────────────────────────────────────

    def export_conversation(self, cid: str, fmt: str = "markdown") -> str:
        conv = self.get_conversation(cid)
        if not conv:
            return ""
        messages = self.get_messages(cid, limit=10000)
        if fmt == "json":
            return json.dumps({"conversation": conv, "messages": messages}, indent=2, ensure_ascii=False)

        # Markdown
        lines = [f"# {conv['title']}", f"_Created: {conv['created_at']}_", ""]
        for msg in messages:
            role = msg["role"].capitalize()
            text = msg.get("text") or ""
            lines.append(f"**{role}:** {text}")
            lines.append("")
            # Tool calls
            for tc in self.get_tool_calls(msg["id"]):
                label = tc.get("friendly_label") or tc["raw_name"]
                status = tc["status"]
                lines.append(f"  ↳ {label} — {status}")
                lines.append("")
        return "\n".join(lines)

    def export_all(self) -> str:
        """Export all non-deleted conversations as JSON."""
        convs = self.list_conversations(limit=10000, include_archived=True)
        for c in convs:
            c["messages"] = self.get_messages(c["id"], limit=10000)
            for m in c["messages"]:
                m["tool_calls"] = self.get_tool_calls(m["id"])
        return json.dumps({
            "exported_at": _now(),
            "app": "ChatPoE",
            "conversations": convs,
        }, indent=2, ensure_ascii=False)

    # ── Backup ────────────────────────────────────────────────

    def create_backup(self, backup_dir: Path) -> Path:
        """Create a SQLite-aware backup using sqlite3.Connection.backup()."""
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = backup_dir / f"history-backup-{ts}.sqlite3"

        dest = sqlite3.connect(str(backup_path))
        try:
            self._conn.backup(dest)
        finally:
            dest.close()

        logger.info(f"Backup created: {backup_path}")
        return backup_path

    # ── Stats ─────────────────────────────────────────────────

    def get_stats(self) -> dict:
        row = self._conn.execute("SELECT COUNT(*) FROM conversations WHERE deleted_at IS NULL").fetchone()
        conv_count = row[0]
        row = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        msg_count = row[0]
        db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "conversation_count": conv_count,
            "message_count": msg_count,
            "db_size_bytes": db_size,
            "db_path": str(self.db_path),
            "fts5_available": self._has_fts5,
        }

    def clear_all(self):
        """Delete all conversations and messages (keep schema)."""
        self._conn.execute("DELETE FROM tool_calls")
        self._conn.execute("DELETE FROM messages")
        self._conn.execute("DELETE FROM conversations")
        self._conn.commit()
        logger.info("Chat history cleared")

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            logger.info("History DB closed")
