"""Generate simulated traffic against a running RecallDeck server.

Simulated traffic is explicitly allowed for this hackathon track: the app
only has four days to collect data, so a scripted stream of sessions with
one stable id per visitor seeds the experiment. The behaviour rates below
are assumptions, not findings: recall-first visitors are simulated as
retaining more (62% retest correct) than re-read visitors (48%), and about
8% of sessions are abandoned part-way. Real classmates using the app will
replace this data over time.

Usage:
    python scripts/simulate_traffic.py [--base-url URL] [--visitors N]
                                       [--seed N]
"""
import argparse
import random
import uuid

import httpx

# Simulated behaviour rates (assumptions, documented in the README).
RECALL_RETEST_CORRECT = 0.62   # recall-first visitors, retest correct
REREAD_RETEST_CORRECT = 0.48   # re-read visitors, retest correct
RECALL_STUDY_KNOW = 0.60      # recall-first visitors saying "I know it"
ABANDON_RATE = 0.08            # sessions quit before 10 cards


def run_session(client: httpx.Client, visitor_id: str, rng: random.Random) -> str:
    """Run one session for one visitor. Returns 'completed' or 'abandoned'."""
    start = client.post(
        "/api/session/start", json={"visitor_id": visitor_id},
        headers={"X-Visitor-Id": visitor_id},
    )
    start.raise_for_status()
    data = start.json()
    session_id = data["session_id"]
    variation = data["variation"]
    cards = data["cards"]

    abandon_at = None
    if rng.random() < ABANDON_RATE:
        abandon_at = rng.randint(3, 7)

    for pos, card in enumerate(cards):
        if abandon_at is not None and pos >= abandon_at:
            client.post(f"/api/session/{session_id}/abandon")
            return "abandoned"
        if card["kind"] == "retest":
            rate = (RECALL_RETEST_CORRECT if variation == "recall_first"
                    else REREAD_RETEST_CORRECT)
            correct = rng.random() < rate
        elif variation == "recall_first":
            correct = rng.random() < RECALL_STUDY_KNOW
        else:
            correct = None  # re-read: the visitor just flips through
        client.post(f"/api/session/{session_id}/answer",
                    json={"card_id": card["card_id"], "correct": correct})

    client.post(f"/api/session/{session_id}/complete")
    return "completed"


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate RecallDeck traffic.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--visitors", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    rng = random.Random(args.seed)

    results = {"completed": 0, "abandoned": 0}
    with httpx.Client(base_url=args.base_url, timeout=30,
                      trust_env=False) as client:  # ignore proxy env vars (localhost)
        for _ in range(args.visitors):
            visitor_id = str(uuid.uuid4())
            n_sessions = rng.choice([1, 2, 2, 3])
            for _ in range(n_sessions):
                results[run_session(client, visitor_id, rng)] += 1

        stats = client.get("/api/stats").json()

    print(f"visitors: {args.visitors}")
    print(f"sessions: completed={results['completed']}, "
          f"abandoned={results['abandoned']}")
    for variation, s in stats["by_variation"].items():
        print(f"{variation}: sessions={s['sessions']} "
              f"abandoned={s['abandoned']} "
              f"retest={s['retest_correct']}/{s['retest_attempts']} "
              f"rate={s['retest_correct_rate']}")


if __name__ == "__main__":
    main()
