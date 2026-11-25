/* RecallDeck frontend. Vanilla JS, no framework.
 * The visitor id is generated once and stored in localStorage, then sent
 * with every request so the server (and Flagsmith) always see the same id.
 * The variation is fixed per visitor: the server stores it on first sight.
 */
const API = "";
let visitorId = localStorage.getItem("recalldeck_visitor_id");
if (!visitorId) {
  visitorId = crypto.randomUUID();
  localStorage.setItem("recalldeck_visitor_id", visitorId);
}

let session = null;      // {session_id, variation, cards}
let index = 0;           // position in session.cards
let answered = false;    // current card already answered
let sessionDone = false;

const $ = (id) => document.getElementById(id);

function headers() {
  return { "Content-Type": "application/json", "X-Visitor-Id": visitorId };
}

async function api(path, method, body) {
  const res = await fetch(API + path, {
    method: method || "GET",
    headers: headers(),
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error("request failed: " + res.status);
  return res.json();
}

function show(id) {
  for (const s of ["screen-start", "screen-study", "screen-done"]) {
    $(s).hidden = s !== id;
  }
}

function setDebug() {
  const short = visitorId.slice(0, 8);
  if (!session) {
    $("debug-text").textContent = "visitor: " + short + " · not in a session yet.";
    return;
  }
  $("debug-text").textContent =
    "variation: " + session.variation +
    " · visitor: " + short +
    " · session: " + session.session_id.slice(0, 8);
}

function renderCard() {
  const card = session.cards[index];
  answered = false;
  $("progress-text").textContent =
    "Card " + (index + 1) + " of " + session.cards.length;
  const fill = $("progress-fill");
  if (fill) {
    fill.style.width = ((index + 1) / session.cards.length) * 100 + "%";
  }
  $("card-topic").textContent = card.topic + (card.kind === "retest" ? " · retest" : "");
  $("card-question").textContent = card.question;
  $("card-hint").hidden = true;
  $("card-hint").textContent = "Hint: " + card.hint;
  $("btn-hint").hidden = false;
  $("card-answer").hidden = true;
  $("card-answer").textContent = "";
  $("controls-recall").hidden = true;
  $("controls-reread").hidden = true;
  $("controls-next").hidden = true;

  // Retest cards are always recall-style: the retest measures retention.
  // Study cards follow the visitor's variation.
  const recallMode = card.kind === "retest" || session.variation === "recall_first";
  if (recallMode) {
    $("controls-recall").hidden = false;
  } else {
    // reread: the answer is already in the start payload.
    $("card-answer").textContent = card.answer;
    $("card-answer").hidden = false;
    $("controls-reread").hidden = false;
  }
  setDebug();
}

async function submitAnswer(correct) {
  if (answered) return;
  answered = true;
  const card = session.cards[index];
  const data = await api("/api/session/" + session.session_id + "/answer", "POST", {
    card_id: card.card_id,
    correct: correct,
  });
  $("card-answer").textContent = data.answer;
  $("card-answer").hidden = false;
  $("controls-recall").hidden = true;
  $("controls-reread").hidden = true;
  $("controls-next").hidden = false;
}

async function nextCard() {
  index += 1;
  if (index >= session.cards.length) {
    await finishSession();
    return;
  }
  renderCard();
}

async function finishSession() {
  sessionDone = true;
  const data = await api("/api/session/" + session.session_id + "/complete", "POST");
  let text = "You answered " + data.answered + " of " + data.total + " cards.";
  if (data.retest_total > 0) {
    text += " Retest: " + data.retest_correct + " of " + data.retest_total +
      " correct from earlier sessions.";
  } else {
    text += " Next session, 5 cards will be a retest of what you saw today.";
  }
  $("done-summary").textContent = text;
  show("screen-done");
  setDebug();
}

async function startSession() {
  const data = await api("/api/session/start", "POST", { visitor_id: visitorId });
  session = data;
  index = 0;
  sessionDone = false;
  show("screen-study");
  renderCard();
}

async function quitSession() {
  if (session && !sessionDone) {
    try {
      await api("/api/session/" + session.session_id + "/abandon", "POST");
    } catch (e) { /* quitting must never fail loudly */ }
  }
  session = null;
  show("screen-start");
  setDebug();
}

// If the tab closes mid-session, the session counts as abandoned.
window.addEventListener("beforeunload", () => {
  if (session && !sessionDone) {
    navigator.sendBeacon(
      "/api/session/" + session.session_id + "/abandon",
      new Blob([], { type: "application/json" })
    );
  }
});

$("btn-start").addEventListener("click", startSession);
$("btn-again").addEventListener("click", startSession);
$("btn-home").addEventListener("click", () => { session = null; show("screen-start"); setDebug(); });
$("btn-quit").addEventListener("click", quitSession);
$("btn-hint").addEventListener("click", () => {
  $("card-hint").hidden = false;
  $("btn-hint").hidden = true;
});
$("btn-know").addEventListener("click", () => submitAnswer(true));
$("btn-dont").addEventListener("click", () => submitAnswer(false));
$("btn-next-reread").addEventListener("click", () => submitAnswer(null));
$("btn-next").addEventListener("click", nextCard);

setDebug();
