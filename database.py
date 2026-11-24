"""SQLite storage for RecallDeck.

Tables:
- visitors: one row per stable visitor id, with the Flagsmith variation served.
- sessions: one row per study session (10 cards).
- session_cards: the cards in a session, kind 'study' or 'retest',
  with the visitor's self-reported answer (correct = 1/0, NULL = only seen).
- events: every experiment event, also mirrored to Flagsmith.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS visitors (
    visitor_id TEXT PRIMARY KEY,
    variation TEXT NOT NULL,
    first_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    visitor_id TEXT NOT NULL,
    variation TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    abandoned INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (visitor_id) REFERENCES visitors(visitor_id)
);
CREATE TABLE IF NOT EXISTS session_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    card_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL,
    correct INTEGER,
    answered_at TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    visitor_id TEXT NOT NULL,
    session_id TEXT,
    event_name TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL mode: commits write to the WAL instead of fsyncing the main file
    # on every commit. On this VM a plain commit costs ~90ms; WAL makes it
    # ~1ms, which matters because each request does several commits.
    # synchronous=NORMAL in WAL mode is the standard safe setting for this.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_or_create_visitor(conn: sqlite3.Connection, visitor_id: str, variation: str) -> dict:
    row = conn.execute(
        "SELECT visitor_id, variation FROM visitors WHERE visitor_id = ?", (visitor_id,)
    ).fetchone()
    if row:
        return dict(row)
    conn.execute(
        "INSERT INTO visitors (visitor_id, variation, first_seen) VALUES (?, ?, ?)",
        (visitor_id, variation, _now()),
    )
    conn.commit()
    return {"visitor_id": visitor_id, "variation": variation}


def create_session(conn: sqlite3.Connection, session_id: str, visitor_id: str,
                   variation: str, cards: list[dict]) -> None:
    """cards: list of {'card_id': str, 'kind': 'study'|'retest'} in position order."""
    conn.execute(
        "INSERT INTO sessions (session_id, visitor_id, variation, started_at)"
        " VALUES (?, ?, ?, ?)",
        (session_id, visitor_id, variation, _now()),
    )
    for pos, card in enumerate(cards):
        conn.execute(
            "INSERT INTO session_cards (session_id, card_id, position, kind)"
            " VALUES (?, ?, ?, ?)",
            (session_id, card["card_id"], pos, card["kind"]),
        )
    conn.commit()


def get_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return dict(row) if row else None


def get_session_cards(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT card_id, position, kind, correct, answered_at FROM session_cards"
        " WHERE session_id = ? ORDER BY position",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def record_answer(conn: sqlite3.Connection, session_id: str, card_id: str,
                  correct: bool | None) -> dict | None:
    """Record the visitor's answer for one card. Returns the card row, or None."""
    row = conn.execute(
        "SELECT * FROM session_cards WHERE session_id = ? AND card_id = ?",
        (session_id, card_id),
    ).fetchone()
    if row is None:
        return None
    if row["correct"] is not None:
        return dict(row)  # already answered: do not overwrite, stay idempotent
    conn.execute(
        "UPDATE session_cards SET correct = ?, answered_at = ?"
        " WHERE session_id = ? AND card_id = ?",
        (None if correct is None else int(correct), _now(), session_id, card_id),
    )
    conn.commit()
    return dict(row)


def complete_session(conn: sqlite3.Connection, session_id: str) -> bool:
    cur = conn.execute(
        "UPDATE sessions SET completed = 1 WHERE session_id = ? AND completed = 0",
        (session_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def abandon_session(conn: sqlite3.Connection, session_id: str) -> bool:
    """Mark a session abandoned. Only counts if it was not completed."""
    cur = conn.execute(
        "UPDATE sessions SET abandoned = 1 WHERE session_id = ?"
        " AND completed = 0 AND abandoned = 0",
        (session_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def record_event(conn: sqlite3.Connection, visitor_id: str, session_id: str | None,
                 event_name: str, payload: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO events (visitor_id, session_id, event_name, payload, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (visitor_id, session_id, event_name,
         json.dumps(payload or {}), _now()),
    )
    conn.commit()


def seen_study_card_ids(conn: sqlite3.Connection, visitor_id: str) -> set[str]:
    """Card ids the visitor has seen as study cards in earlier sessions."""
    rows = conn.execute(
        "SELECT DISTINCT sc.card_id FROM session_cards sc"
        " JOIN sessions s ON s.session_id = sc.session_id"
        " WHERE s.visitor_id = ? AND sc.kind = 'study'",
        (visitor_id,),
    ).fetchall()
    return {r["card_id"] for r in rows}


def retested_card_ids(conn: sqlite3.Connection, visitor_id: str) -> set[str]:
    """Card ids the visitor has already answered as retest cards."""
    rows = conn.execute(
        "SELECT DISTINCT sc.card_id FROM session_cards sc"
        " JOIN sessions s ON s.session_id = sc.session_id"
        " WHERE s.visitor_id = ? AND sc.kind = 'retest' AND sc.correct IS NOT NULL",
        (visitor_id,),
    ).fetchall()
    return {r["card_id"] for r in rows}


def stats(conn: sqlite3.Connection) -> dict:
    """Aggregate experiment numbers, split by variation."""
    out: dict = {"total_visitors": 0, "total_sessions": 0, "by_variation": {}}
    out["total_visitors"] = conn.execute(
        "SELECT COUNT(*) FROM visitors").fetchone()[0]
    out["total_sessions"] = conn.execute(
        "SELECT COUNT(*) FROM sessions").fetchone()[0]
    for variation in ("recall_first", "reread"):
        sessions = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE variation = ?", (variation,)
        ).fetchone()[0]
        completed = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE variation = ? AND completed = 1",
            (variation,)).fetchone()[0]
        abandoned = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE variation = ? AND abandoned = 1",
            (variation,)).fetchone()[0]
        retest = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(correct), 0) FROM session_cards sc"
            " JOIN sessions s ON s.session_id = sc.session_id"
            " WHERE s.variation = ? AND sc.kind = 'retest' AND sc.correct IS NOT NULL",
            (variation,)).fetchone()
        retest_attempts, retest_correct = retest[0], retest[1]
        out["by_variation"][variation] = {
            "sessions": sessions,
            "completed": completed,
            "abandoned": abandoned,
            "abandonment_rate": round(abandoned / sessions, 3) if sessions else None,
            "retest_attempts": retest_attempts,
            "retest_correct": retest_correct,
            "retest_correct_rate": (
                round(retest_correct / retest_attempts, 3) if retest_attempts else None
            ),
        }
    return out


def db_path_from_env(default: str = "recalldeck.db") -> str:
    return os.environ.get("RECALLDECK_DB", default)
