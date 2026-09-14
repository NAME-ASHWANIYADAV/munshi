# MunshiJi — Architecture

> The AI *munshi* for Indian merchants. Built for the Paytm Build for India AI Hackathon,
> Delhi Edition 2026 — Track 1, Merchant Growth AI — by team Neural Wave.

---

## 1. The idea in one paragraph

Paytm gave India's merchants the data. Nobody gave them back the *munshi* — the trusted bookkeeper
who kept the khata, knew every customer by name, and told the owner plainly what to do about it.
MunshiJi is that munshi, as an AI teammate the merchant simply talks to. It **knows** the shop
(a knowledge graph over transactions, customers, inventory and udhaar), **advises** on it
(statistical insight engines, not vibes), and — with permission — **acts** on it (a tool-calling
agent that sends the win-back offer, chases the udhaar, drafts the restock order).

The property that separates it from a chatbot is memory across sessions: an action taken in
Monday's call is recalled, with its measured outcome, in Tuesday's.

---

## 2. System shape

```
              ┌──────────────────── merchant speaks (Hindi / Hinglish) ─────────────────┐
              │                                                                          │
              ▼                                                                          │
   ┌─────────────────────┐      ┌──────────────────────┐      ┌───────────────────────┐  │
   │  Sarvam  saaras:v3  │─────▶│      Agent loop      │◀────▶│  Cognee graph memory  │  │
   │  speech-to-text     │      │  (tools + approval)  │      │  n8n-nodes-cognee     │  │
   └─────────────────────┘      └──────────┬───────────┘      └───────────────────────┘  │
                                            │                                             │
   ┌─────────────────────┐                  │                 ┌───────────────────────┐  │
   │  Sarvam sarvam-105b │◀─────────────────┤                 │  Insight engines      │  │
   │  reasoning + tools  │                  │◀────────────────│  (robust statistics)  │  │
   └─────────────────────┘                  │                 └───────────────────────┘  │
                                            │                                             │
   ┌─────────────────────┐      ┌───────────▼──────────┐      ┌───────────────────────┐  │
   │  Sarvam  bulbul:v3  │◀─────│   approval gate      │─────▶│  n8n orchestration    │──┘
   │  text-to-speech     │      │  (merchant says haan)│      │  WhatsApp · links     │
   └─────────────────────┘      └──────────────────────┘      └───────────────────────┘
```

Every box has **two implementations behind one interface**: a live vendor client and a fully
functional offline one. That decision is described in §4 and is the reason this demo cannot fail.

---

## 3. The three sponsor integrations, and why each is load-bearing

| Sponsor | Where it sits | Why it is not decoration |
|---|---|---|
| **Sarvam AI** | The entire voice + reasoning loop: `saaras:v3` streaming STT → `sarvam-105b` tool-calling LLM → `bulbul:v3` streaming TTS | The product *is* a conversation in Hindi/Hinglish. A merchant who does not read English dashboards is the user. Remove Sarvam and there is no product — only a dashboard. Sarvam Voice Agents' HTTPS API tools are the documented mechanism by which the agent reaches n8n. |
| **n8n** | The action layer. Every approved action becomes a webhook call into a workflow that fans out to WhatsApp/SMS, payment links and supplier email, and reports delivery back | This is the "kar deta hai" half. Without it MunshiJi *advises* and stops — which is exactly the chatbot we are trying not to build. n8n also owns the approval gate's branching and notification paths. |
| **Cognee** | Long-term memory: the merchant's world as a knowledge graph, queried GraphRAG-style | The demo's kill shot — conversation #2 recalling conversation #1's action *and its outcome* — is a graph traversal, not a text match. Plain RAG returns "text that looked similar"; the graph returns the relationships (`action -targeted-> customer`, `action -recovered-> ₹2,340`). |

Every integration point above is **vendor-documented leg by leg**. We do not claim an official
end-to-end reference stack, because there isn't one — we assembled it.

---

## 4. The dual-provider rule (the most important engineering decision)

```
providers/llm.py     → LLMProvider     → SarvamLLM     | LocalLLM
providers/stt.py     → STTProvider     → SarvamSTT     | LocalSTT
providers/tts.py     → TTSProvider     → SarvamTTS     | LocalTTS
providers/memory.py  → MemoryProvider  → CogneeMemory  | LocalGraphMemory
providers/actions.py → ActionProvider  → N8nActions    | LocalActions
```

`providers/factory.py` picks per capability:

* `MUNSHIJI_PROVIDER_MODE=local` — always offline.
* `=live` — always vendor; a failed probe is surfaced loudly, never masked.
* `=auto` *(default)* — vendor when its credential is present **and** an async health probe passes,
  otherwise fall back to local with a logged warning.

Construction never touches the network, so imports are cheap and the test suite is hermetic.
Probing is one explicit awaited step at application startup.

**Why this matters more than one extra feature would have.** Venue Wi-Fi dies. Keys expire.
Vendors rate-limit mid-demo. A judging slot is eight minutes long. A system that degrades to a
working offline path is worth more than one that is five percent more impressive and occasionally
shows a stack trace. It also makes every layer testable in CI, which is why the suite runs offline
in under a minute.

**Local does not mean fake** (SPEC §2.2). `LocalLLM` is a real Indic intent router plus a bilingual
composer working from real tool results. `LocalGraphMemory` is a real knowledge graph with real
BM25 retrieval and real multi-hop traversal. `LocalActions` really advances the action state machine
and really writes outcome rows. Every number spoken or displayed was computed from the database.

---

## 5. Layers

### 5.1 Data (`db/`, `repositories/`, `seed/`)

Thirteen tables covering the merchant's world: merchant, customers, products, transactions and
transaction *items*, khata (udhaar) entries, insights, action requests and their measured outcomes,
conversations and turns, and the memory graph's nodes and edges.

Two invariants worth calling out:
* **Money is `int` paise everywhere.** Never a float. Indian formatting (lakh/crore) happens once,
  in `money.py`, and crosses the API as both `paise` and a pre-formatted `display` string.
* **Time is IST.** Timestamps persist as timezone-aware UTC; every business-day question is answered
  through `clock.py`. "Aaj" means the Indian calendar day — 19:00 UTC is already tomorrow in Delhi.

`seed/` generates 180 days of statistically realistic history for one kirana: day-of-week
seasonality, a bimodal intraday curve, salary-week uplift, festival ramps, ~220 customers each with
their **own visit cadence**, and a drifting payment mix. It plants discoverable signals — dormant
regulars, dead stock, an imminent stockout, aged udhaar, a soft collection dip — which the insight
engines then have to actually find. The generator is deterministic under a seed, so every demo is
identical.

### 5.2 Intelligence (`insights/`)

Engines, not heuristics. The ones that carry the demo:

* **Collection anomaly** — the weekday baseline is a **median + MAD** over the trailing eight weeks
  of the *same weekday*, flagged on a robust z-score. A mean-and-standard-deviation version would be
  dragged around by one festival Saturday.
* **Partial-day projection** — from the historical intraday cumulative curve, today's close is
  projected from collections so far, with a confidence band, and explicitly refuses to project
  before enough of the day has elapsed. This is what makes a mid-afternoon demo feel alive.
* **Per-customer cadence dormancy** — a customer is dormant when they are late *by their own
  standards* (`days_since_last > median_gap + 1.5 × IQR` for that customer), not against a global
  30-day rule. A global rule flags the daily shopper who skipped a weekend and misses the monthly
  regular who has vanished. This one design choice is the difference between a useful win-back list
  and a spam list.
* **Inventory** — EWMA consumption, days-of-cover against lead time, dead stock by locked capital,
  expiry risk for perishables, restock sizing with festival uplift.
* **Credit** — aging buckets, a per-customer reliability score from settle history, and a chase
  priority that decides *who* to remind and in *what tone*.

Each finding is bilingual, scored (`severity × confidence × log1p(impact)` with recency decay), and
carries a `suggested_tool` + `suggested_params` — so a finding converts to a proposed action in one
conversational step rather than a menu.

### 5.3 Memory (`memory/`)

Ingest turns DB state into typed nodes and edges: days, customers, products, insights, conversations
and — critically — **actions with their outcomes**. Retrieval is GraphRAG-shaped: a hand-rolled BM25
index (tokenising Devanagari and Latin together, plus digits, so `"18540"` and `"₹18,540"` both
match) finds seed nodes, then the graph is expanded one to two hops with edge-weight and recency
decay, and every hit carries a readable provenance path.

The live path maps the same three operations onto Cognee (`add` → `cognify` → `search`), including
its n8n-verified node, so the story told on stage is the story in the code.

### 5.4 Agent (`agent/`)

`loop.py` runs one turn: recall memory → assemble prompt (persona + live snapshot + memory +
pending approval) → model call → tool calls (bounded iterations) → compose → persist → re-ingest.

`approval.py` owns the safety boundary, deliberately separated from the loop so it cannot be talked
around by a prompt:
* a typed `ActionStatus` state machine with explicit allowed transitions,
* a hard refusal to execute anything not in `APPROVED`,
* a daily outbound cap and a per-khata reminder cooldown,
* a full audit trail: who proposed, what, when decided, what result, what measured outcome.

The registry cross-checks every tool's `requires_approval` flag against `approval.WRITE_TOOLS` at
registration time, so the two lists cannot silently drift apart.

### 5.5 Interface (`api/`, `frontend/`)

FastAPI with a server-sent-event stream so the companion screen moves while the merchant is
mid-sentence. The screen is deliberately a *witness*: the tool trace, the provider badges
(live vs local, per sponsor), the pending-approval state and the memory graph are all visible, so
the audience can see the system think rather than take our word for it.

---

## 6. Product-safety choices

These are decisions, not omissions, and they are defensible in the room:

1. **Nothing outbound without an explicit yes.** Not a setting; a state machine.
2. **Reminders top out at a `FIRM` register** — clear and direct, never threatening, never
   mentioning consequences or shaming. Collections software is where fintech products cause real
   harm; we picked the ceiling deliberately and test for forbidden vocabulary.
3. **Rate limits in the domain layer**, not the UI: one reminder per khata entry per week, a daily
   outbound cap per merchant.
4. **No invented numbers.** The composer may only quote figures present in tool results; the tests
   assert that no unsourced rupee figure appears in a reply.
5. **Expiring approvals** — an unanswered proposal expires rather than firing hours later.

---

## 7. Known limits

* Single-merchant, single-process: the event bus is in-memory, and SQLite is the default store.
  Both are correct for a demo and both sit behind interfaces that would swap for Redis/Postgres.
* Local LLM mode is an intent router, not a general reasoner — it handles the merchant-copilot
  intent space well and will say so when it does not understand, rather than guessing.
* Vendor capability claims come from vendor documentation; real-world ASR accuracy on a noisy demo
  floor is unverified, which is precisely why the offline path exists.
* Outcome simulation (redemptions arriving after an offer) is derived from real customer segments
  and real historical ticket sizes, but it is a simulation, and the UI says so.
