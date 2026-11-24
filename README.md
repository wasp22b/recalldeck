# RecallDeck

A flashcard study app that runs a real Flagsmith A/B test. Built for the
WeMakeDevs **Toggle Wars: The Flagsmith Experimentation Hackathon**,
Track 01 — Test Something Real.

**Hypothesis (one sentence):** forcing recall before revealing the answer
raises next-session retention vs re-reading, without raising abandonment.

## What it is

- 60 flashcards on AI basics — agents (PEAS, agent types), search (BFS, DFS,
  iterative deepening, A*, minimax, alpha-beta, hill climbing), logic
  (propositional logic, FOL, resolution, unification, Wumpus world) — written
  from the author's Semester 1 MSc AI study notes. The deck is the content
  moat: real cards a first-semester MSc AI student would actually use.
- One session is 10 cards. From the second session on, the first 5 cards are
  a **retest** of cards seen in earlier sessions; the other 5 are new.
- Retest cards are always shown recall-style (question first, commit, then
  the answer), because the retest is the measurement instrument, not the
  treatment.

## How Flagsmith is used

- **Flag:** `study_mode` (multivariate) in the RecallDeck project,
  Development environment. Variations: `recall_first` (50%) and `reread`
  (50%). Created in the Flagsmith dashboard; this repo creates no flags.
- `recall_first`: the study card shows the question only. The student must
  click **I know it** or **I don't know it** before the answer is revealed.
- `reread`: the study card shows the question and the answer together, and
  the student flips through.
- **Stable visitor id:** generated once per visitor, stored in `localStorage`,
  and sent as `X-Visitor-Id` on every request. The same id always gets the
  same variation. A fresh id per request would invalidate the experiment.
- **Fallback:** the Python SDK runs with `enable_local_evaluation=True` and a
  `default_flag_handler` that serves `reread` if Flagsmith is unreachable,
  so the app keeps working offline.
- **Metrics** (every event goes to Flagsmith with `track_event` **and** to
  SQLite, so data is never lost):
  - Primary: `retest_correct` — a retest card answered correctly.
  - Guardrail: `session_abandoned` — a session quit before 10 cards.
  - Also tracked: `session_started`, `session_completed`, `card_answered`.
- The **debug footer** at the bottom of the page shows the variation served
  and a short visitor id, so the demo can show it live.

## Setup

```bash
cd app
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# Put the Flagsmith server-side environment key in .env (never commit it):
echo "FLAGSMITH_ENVIRONMENT_KEY=<your Development server-side key>" > .env
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. The server creates `recalldeck.db` (SQLite) on
first start. `.env` and `*.db` are gitignored.

## Traffic script

Simulated traffic is explicitly allowed for this track: the app has four
days to collect data, so a scripted stream of sessions with one stable id
per visitor seeds the experiment.

```bash
.venv/bin/python scripts/simulate_traffic.py --visitors 200 --seed 42
```

It prints a summary and the live numbers from `/api/stats`.

## Results

Results are read from `GET /api/stats` (and from the Flagsmith
Experimentation dashboard, which shows the same events).

| Variation | Sessions | Retest correct | Retest rate | Abandoned |
|-----------|----------|----------------|-------------|-----------|
| recall_first | 190 | 301 / 475 | 0.634 | 12 (6.3%) |
| reread | 214 | 240 / 537 | 0.447 | 15 (7.0%) |

Seeded with `scripts/simulate_traffic.py --visitors 200 --seed 42`
(404 sessions: 377 completed, 27 abandoned). These are simulated numbers,
not real students — see the note below.

**How to read the result honestly:** the Flagsmith Experimentation view
shows a win probability and a credible interval. An 84% win probability
whose interval crosses zero is **not** a win — it means the data is not
conclusive yet. Report the interval, not just the headline number.

**About the simulated traffic:** the traffic script uses assumed behaviour
rates (recall-first retest correct 62%, re-read 48%, ~8% of sessions
abandoned). Those rates are assumptions used to seed the experiment, not
findings. Real students using the app replace this data over time; the
README will be updated with real numbers as they arrive.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

Covers session flow (10 cards, first session all study), retest selection
(second session retests cards from the first), stable variation per visitor,
both variations being served, retest-correct recording, the abandonment
guardrail, idempotent answers, and the Flagsmith fallback when the SDK is
unreachable.

## Project layout

```
app/
  main.py              FastAPI app + endpoints
  database.py          SQLite schema + queries
  flagsmith_client.py  Flagsmith SDK wrapper (local evaluation + fallback)
  deck.json            60 flashcards (agents / search / logic)
  static/              vanilla JS single-page frontend
  scripts/simulate_traffic.py
  tests/               pytest suite
  requirements.txt
  .env                 (gitignored) FLAGSMITH_ENVIRONMENT_KEY
  recalldeck.db        (gitignored) SQLite data
```
