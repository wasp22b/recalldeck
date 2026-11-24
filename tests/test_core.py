"""Core-logic tests for RecallDeck: session flow, retest selection,
flag fallback, and the abandonment guardrail."""
import json

import database as db
import main
from conftest import TEST_DB, answer_all
from flagsmith_client import FALLBACK_VARIATION, FlagsmithClient


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["deck_cards"] == 60
    assert r.json()["flag"] == "study_mode"


def test_deck_has_60_real_cards():
    deck = json.loads((main.BASE_DIR / "deck.json").read_text())
    assert len(deck) == 60
    assert len({c["id"] for c in deck}) == 60
    for card in deck:
        assert card["question"] and card["answer"] and card["hint"]
        assert card["topic"] in ("agents", "search", "logic")


def test_first_session_has_10_study_cards(client):
    r = client.post("/api/session/start", json={"visitor_id": "v-first"})
    assert r.status_code == 200
    data = r.json()
    assert len(data["cards"]) == 10
    assert all(c["kind"] == "study" for c in data["cards"])
    assert data["variation"] in ("recall_first", "reread")
    # reread study cards reveal the answer up front; recall cards do not.
    if data["variation"] == "reread":
        assert all(c["answer"] for c in data["cards"])
    else:
        assert all(c["answer"] is None for c in data["cards"])


def test_same_visitor_same_variation(client):
    v1 = client.post("/api/session/start", json={"visitor_id": "v-stable"}).json()
    answer_all(client, v1["session_id"], v1["cards"], v1["variation"])
    assert client.post(f"/api/session/{v1['session_id']}/complete").status_code == 200
    v2 = client.post("/api/session/start", json={"visitor_id": "v-stable"}).json()
    assert v2["variation"] == v1["variation"]


def test_both_variations_are_served(client):
    seen = set()
    for i in range(100):
        r = client.post("/api/session/start", json={"visitor_id": f"v-{i}"})
        seen.add(r.json()["variation"])
        assert r.status_code == 200
    assert seen == {"recall_first", "reread"}


def test_second_session_retests_first_session_cards(client):
    s1 = client.post("/api/session/start", json={"visitor_id": "v-retest"}).json()
    answer_all(client, s1["session_id"], s1["cards"], s1["variation"])
    assert client.post(f"/api/session/{s1['session_id']}/complete").status_code == 200

    s2 = client.post("/api/session/start", json={"visitor_id": "v-retest"}).json()
    assert len(s2["cards"]) == 10
    retest = [c for c in s2["cards"] if c["kind"] == "retest"]
    new = [c for c in s2["cards"] if c["kind"] == "study"]
    assert len(retest) == 5
    assert len(new) == 5
    first_ids = {c["card_id"] for c in s1["cards"]}
    assert {c["card_id"] for c in retest} <= first_ids
    assert {c["card_id"] for c in new}.isdisjoint(first_ids)
    # Retest cards never reveal the answer up front, in either variation.
    assert all(c["answer"] is None for c in retest)


def test_retest_correct_is_recorded(client):
    s1 = client.post("/api/session/start", json={"visitor_id": "v-metric"}).json()
    answer_all(client, s1["session_id"], s1["cards"], s1["variation"])
    client.post(f"/api/session/{s1['session_id']}/complete")

    s2 = client.post("/api/session/start", json={"visitor_id": "v-metric"}).json()
    retest = [c for c in s2["cards"] if c["kind"] == "retest"]
    assert len(retest) == 5
    for card in retest:
        r = client.post(f"/api/session/{s2['session_id']}/answer",
                        json={"card_id": card["card_id"], "correct": True})
        assert r.status_code == 200
        assert r.json()["answer"]  # the answer is revealed after committing
    stats = client.get("/api/stats").json()
    var_stats = stats["by_variation"][s2["variation"]]
    assert var_stats["retest_attempts"] == 5
    assert var_stats["retest_correct"] == 5
    assert var_stats["retest_correct_rate"] == 1.0


def test_abandon_is_recorded_as_guardrail(client):
    s1 = client.post("/api/session/start", json={"visitor_id": "v-quit"}).json()
    for card in s1["cards"][:3]:
        client.post(f"/api/session/{s1['session_id']}/answer",
                    json={"card_id": card["card_id"], "correct": True})
    r = client.post(f"/api/session/{s1['session_id']}/abandon")
    assert r.json()["abandoned"] is True
    stats = client.get("/api/stats").json()
    assert stats["by_variation"][s1["variation"]]["abandoned"] == 1
    # An abandoned session cannot be completed afterwards.
    assert client.post(f"/api/session/{s1['session_id']}/complete").status_code == 409


def test_answer_is_idempotent(client):
    s1 = client.post("/api/session/start", json={"visitor_id": "v-twice"}).json()
    card = s1["cards"][0]
    r1 = client.post(f"/api/session/{s1['session_id']}/answer",
                     json={"card_id": card["card_id"], "correct": True})
    r2 = client.post(f"/api/session/{s1['session_id']}/answer",
                     json={"card_id": card["card_id"], "correct": False})
    assert r1.status_code == 200 and r2.status_code == 200
    conn = db.connect(str(TEST_DB))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_name = 'card_answered'"
        ).fetchone()[0]
        row = conn.execute(
            "SELECT correct FROM session_cards WHERE session_id = ? AND card_id = ?",
            (s1["session_id"], card["card_id"]),
        ).fetchone()
    finally:
        conn.close()
    assert n == 1  # the second answer was not recorded again
    assert row["correct"] == 1  # the first answer was not overwritten


def test_flag_fallback_when_flagsmith_is_unavailable():
    # No key and no client: fallback.
    assert FlagsmithClient(environment_key=None, enabled=False).get_variation("x") \
        == FALLBACK_VARIATION
    # A client whose SDK raises: fallback as well.
    client = FlagsmithClient(environment_key="dummy", enabled=False)
    client._client = _RaisingSDK()
    assert client.get_variation("x") == FALLBACK_VARIATION


class _RaisingSDK:
    def get_experiment_flag(self, *args, **kwargs):
        raise RuntimeError("Flagsmith unreachable")

    def track_event(self, *args, **kwargs):
        raise RuntimeError("Flagsmith unreachable")

    def flush_events(self):
        raise RuntimeError("Flagsmith unreachable")
