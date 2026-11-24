"""Test setup: a throwaway SQLite file and a deterministic Flagsmith stub.

The stub assigns variations by hashing the visitor id, so both variations
appear and the same visitor always gets the same one. The real Flagsmith
client is never constructed in tests (FLAGSMITH_ENVIRONMENT_KEY is empty).
"""
import hashlib
import os
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

TEST_DB = APP_DIR / "tests" / "test.db"

os.environ["RECALLDECK_DB"] = str(TEST_DB)
os.environ["FLAGSMITH_ENVIRONMENT_KEY"] = ""

import database as db  # noqa: E402
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class StubFlagsmith:
    """Deterministic stand-in for the real Flagsmith client."""

    def __init__(self):
        self.events: list[tuple] = []
        self.available = True

    def get_variation(self, visitor_id: str) -> str:
        h = int(hashlib.sha256(visitor_id.encode()).hexdigest()[:8], 16)
        return "recall_first" if h % 2 == 0 else "reread"

    def track_event(self, event, visitor_id, value=None, metadata=None):
        self.events.append((event, visitor_id, value, metadata))

    def flush(self):
        pass


@pytest.fixture()
def stub_flagsmith(monkeypatch):
    stub = StubFlagsmith()
    monkeypatch.setattr(main, "flagsmith_client", stub)
    return stub


@pytest.fixture()
def client(stub_flagsmith):
    """Fresh database + test client for every test."""
    if TEST_DB.exists():
        TEST_DB.unlink()
    db.init_db(str(TEST_DB))
    with TestClient(main.app) as c:
        yield c
    if TEST_DB.exists():
        TEST_DB.unlink()


def answer_all(client, session_id, cards, variation):
    """Answer every card in a session the way the UI would."""
    for card in cards:
        if card["kind"] == "retest" or variation == "recall_first":
            correct = True
        else:
            correct = None
        r = client.post(f"/api/session/{session_id}/answer",
                        json={"card_id": card["card_id"], "correct": correct})
        assert r.status_code == 200, r.text
