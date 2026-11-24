"""RecallDeck — flashcard study app with a real Flagsmith A/B test.

Experiment (one sentence): forcing recall before revealing the answer raises
next-session retention vs re-reading, without raising abandonment.

Design:
- Flag ``study_mode`` (multivariate): ``recall_first`` vs ``reread``.
  The same stable visitor id always gets the same variation.
- A session is 10 cards. From the second session on, the first 5 cards are
  a retest of cards seen in earlier sessions; the other 5 are new.
- Retest cards are always shown recall-style (question first, commit, then
  the answer is revealed), because the retest is the measurement instrument,
  not the treatment. Study cards follow the visitor's variation.
- Primary metric: ``retest_correct`` (a retest card answered correctly).
  Guardrail: ``session_abandoned`` (a session quit before 10 cards).
  Events go to Flagsmith AND to SQLite, so data is never lost.
"""
import json
import os
import random
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import database as db
from flagsmith_client import FlagsmithClient

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DECK_PATH = BASE_DIR / "deck.json"
STATIC_DIR = BASE_DIR / "static"
SESSION_SIZE = 10
RETEST_SIZE = 5

with open(DECK_PATH, encoding="utf-8") as f:
    DECK: list[dict] = json.load(f)
DECK_BY_ID = {card["id"]: card for card in DECK}
ALL_CARD_IDS = [card["id"] for card in DECK]

DB_PATH = db.db_path_from_env(str(BASE_DIR / "recalldeck.db"))
db.init_db(DB_PATH)

# Module-level client; tests replace it with a stub.
flagsmith_client = FlagsmithClient()

app = FastAPI(title="RecallDeck", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class StartSessionRequest(BaseModel):
    visitor_id: str | None = None


class AnswerRequest(BaseModel):
    card_id: str
    correct: bool | None = None


def _get_conn():
    return db.connect(DB_PATH)


def _resolve_visitor_id(request: Request, body_id: str | None) -> str:
    return body_id or request.headers.get("X-Visitor-Id") or str(uuid.uuid4())


def _public_card(card: dict, kind: str, reveal_answer: bool) -> dict:
    return {
        "card_id": card["id"],
        "topic": card["topic"],
        "question": card["question"],
        "hint": card["hint"],
        "kind": kind,
        "answer": card["answer"] if reveal_answer else None,
    }


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "deck_cards": len(DECK),
        "flagsmith_available": flagsmith_client.available,
        "flag": "study_mode",
    }


@app.post("/api/session/start")
def start_session(payload: StartSessionRequest, request: Request):
    visitor_id = _resolve_visitor_id(request, payload.visitor_id)
    conn = _get_conn()
    try:
        visitor = conn.execute(
            "SELECT visitor_id, variation FROM visitors WHERE visitor_id = ?",
            (visitor_id,),
        ).fetchone()
        if visitor:
            variation = visitor["variation"]
        else:
            variation = flagsmith_client.get_variation(visitor_id)
            db.get_or_create_visitor(conn, visitor_id, variation)

        # A previous unfinished session means the visitor quit: guardrail.
        unfinished = conn.execute(
            "SELECT session_id FROM sessions WHERE visitor_id = ?"
            " AND completed = 0 AND abandoned = 0",
            (visitor_id,),
        ).fetchall()
        for row in unfinished:
            db.abandon_session(conn, row["session_id"])
            db.record_event(conn, visitor_id, row["session_id"],
                            "session_abandoned",
                            {"reason": "new_session_started"})

        seen = db.seen_study_card_ids(conn, visitor_id)
        retested = db.retested_card_ids(conn, visitor_id)
        retest_pool = sorted(seen - retested)
        new_pool = sorted(set(ALL_CARD_IDS) - seen - retested)

        retest_ids = random.sample(retest_pool, min(RETEST_SIZE, len(retest_pool)))
        remaining = SESSION_SIZE - len(retest_ids)
        new_ids = random.sample(new_pool, min(remaining, len(new_pool)))
        remaining -= len(new_ids)
        if remaining > 0:
            extra_pool = sorted(seen - set(retest_ids) - set(new_ids))
            new_ids += random.sample(extra_pool, min(remaining, len(extra_pool)))

        session_id = str(uuid.uuid4())
        cards = ([{"card_id": c, "kind": "retest"} for c in retest_ids]
                 + [{"card_id": c, "kind": "study"} for c in new_ids])
        db.create_session(conn, session_id, visitor_id, variation, cards)
        db.record_event(conn, visitor_id, session_id, "session_started",
                        {"variation": variation, "cards": len(cards),
                         "retest_cards": len(retest_ids)})
        flagsmith_client.track_event(
            "session_started", visitor_id,
            metadata={"variation": variation, "cards": len(cards)})

        public = []
        for card in cards:
            full = DECK_BY_ID[card["card_id"]]
            # Retest cards are always recall-style (the measurement).
            # Study cards reveal the answer up front only in reread mode.
            reveal = card["kind"] == "study" and variation == "reread"
            public.append(_public_card(full, card["kind"], reveal))
        return {
            "session_id": session_id,
            "visitor_id": visitor_id,
            "variation": variation,
            "cards": public,
        }
    finally:
        conn.close()


@app.post("/api/session/{session_id}/answer")
def answer_card(session_id: str, payload: AnswerRequest):
    card = DECK_BY_ID.get(payload.card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="unknown card")
    conn = _get_conn()
    try:
        session = db.get_session(conn, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        if session["completed"] or session["abandoned"]:
            raise HTTPException(status_code=409, detail="session is finished")
        row = db.record_answer(conn, session_id, payload.card_id, payload.correct)
        if row is None:
            raise HTTPException(status_code=404, detail="card not in this session")
        already_answered = row["correct"] is not None
        if not already_answered:
            visitor_id = session["visitor_id"]
            db.record_event(conn, visitor_id, session_id, "card_answered",
                            {"card_id": payload.card_id, "kind": row["kind"],
                             "correct": payload.correct})
            flagsmith_client.track_event(
                "card_answered", visitor_id,
                metadata={"card_id": payload.card_id, "kind": row["kind"],
                          "correct": payload.correct})
            if row["kind"] == "retest" and payload.correct is True:
                db.record_event(conn, visitor_id, session_id, "retest_correct",
                                {"card_id": payload.card_id})
                flagsmith_client.track_event(
                    "retest_correct", visitor_id, value=1,
                    metadata={"card_id": payload.card_id})
        return {"answer": card["answer"], "hint": card["hint"], "kind": row["kind"]}
    finally:
        conn.close()


@app.post("/api/session/{session_id}/complete")
def complete_session(session_id: str):
    conn = _get_conn()
    try:
        session = db.get_session(conn, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        if session["abandoned"]:
            raise HTTPException(status_code=409, detail="session was abandoned")
        cards = db.get_session_cards(conn, session_id)
        answered = sum(1 for c in cards if c["answered_at"] is not None)
        retest = [c for c in cards if c["kind"] == "retest"]
        retest_correct = sum(1 for c in retest if c["correct"] == 1)
        was_completed = db.complete_session(conn, session_id)
        if was_completed:
            db.record_event(conn, session["visitor_id"], session_id,
                            "session_completed",
                            {"answered": answered, "total": len(cards),
                             "retest_correct": retest_correct,
                             "retest_total": len(retest)})
            flagsmith_client.track_event(
                "session_completed", session["visitor_id"],
                metadata={"answered": answered, "retest_correct": retest_correct})
            # No explicit flush: the SDK's event processor flushes every 10s.
        return {
            "completed": True,
            "answered": answered,
            "total": len(cards),
            "retest_correct": retest_correct,
            "retest_total": len(retest),
        }
    finally:
        conn.close()


@app.post("/api/session/{session_id}/abandon")
def abandon_session(session_id: str):
    conn = _get_conn()
    try:
        session = db.get_session(conn, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        was_abandoned = db.abandon_session(conn, session_id)
        if was_abandoned:
            cards = db.get_session_cards(conn, session_id)
            answered = sum(1 for c in cards if c["answered_at"] is not None)
            db.record_event(conn, session["visitor_id"], session_id,
                            "session_abandoned",
                            {"answered": answered, "total": len(cards)})
            flagsmith_client.track_event(
                "session_abandoned", session["visitor_id"], value=1,
                metadata={"answered": answered, "total": len(cards)})
            # No explicit flush: the SDK's event processor flushes every 10s.
        return {"abandoned": was_abandoned}
    finally:
        conn.close()


@app.get("/api/stats")
def get_stats():
    conn = _get_conn()
    try:
        return db.stats(conn)
    finally:
        conn.close()
