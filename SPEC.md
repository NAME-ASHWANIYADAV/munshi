# MunshiJi — Engineering Specification (v1)

> **This is the contract.** Every module in this repo is written against this document.
> If you are an agent implementing part of this system: read this file fully before writing code,
> and do not invent alternative names, paths, or signatures.

---

## 1. What we are building

**MunshiJi** is a voice-first AI business partner for Indian merchants on the Paytm ecosystem.
The *munshi* was the trusted bookkeeper of the Indian merchant — kept the khata, knew every customer,
and told the owner what to do about it. MunshiJi brings that back, as an AI teammate.

Three capabilities, in ascending order of difficulty:

| Hindi | Meaning | Implementation |
|---|---|---|
| **Jaanta hai** | It *knows* the business | Knowledge graph over transactions, customers, inventory, udhaar |
| **Batata hai** | It *advises* | Insight engines: anomalies, dormancy, dead stock, credit risk |
| **Kar deta hai** | It *acts* | Tool-calling agent → approval gate → n8n execution → outcome tracking |

The differentiator vs. a chatbot: **cross-session memory**. The second conversation knows what the
first one did, and reports the outcome unprompted.

**Hackathon context:** Paytm Build for India AI Hackathon, Delhi Edition, 19 Sep 2026.
Track 1 (Merchant Growth AI). Sponsors whose tech must be visibly load-bearing:
**Sarvam AI** (Indic voice + LLM), **n8n** (orchestration/actions), **Cognee** (graph memory).

---

## 2. Non-negotiable design rules

### 2.1 Dual-mode providers (MOST IMPORTANT RULE)

Every external dependency has **one protocol** and **two implementations**:

```
providers/llm.py     → LLMProvider     → SarvamLLM      | LocalLLM
providers/stt.py     → STTProvider     → SarvamSTT      | LocalSTT
providers/tts.py     → TTSProvider     → SarvamTTS      | LocalTTS
providers/memory.py  → MemoryProvider  → CogneeMemory   | LocalGraphMemory
providers/actions.py → ActionProvider  → N8nActions     | LocalActions
```

* `Live` implementations call the real vendor API.
* `Local` implementations are **deterministic, offline, and fully functional** — not stubs that raise
  `NotImplementedError`. The entire product must work end-to-end with zero API keys and zero internet.
* Selection happens in `providers/factory.py`, driven by `Settings`. Default is `auto`:
  use Live if the credential is present and a health probe passes, else fall back to Local **with a
  logged warning**, never a crash.
* Every provider response carries `provider: Literal["live","local"]` so the UI can show which path served it.

**Why:** venue Wi-Fi dies, API keys expire, vendors rate-limit. A demo that cannot fail is worth more
than a demo that is 5% more impressive. It also makes the whole system testable in CI.

### 2.2 Nothing is fake

Local mode is allowed to be *offline*; it is **not** allowed to be *fake*.
- `LocalLLM` does real intent classification and real template-based Hindi/Hinglish generation over
  **real numbers computed from the database**. It never returns a hardcoded demo string.
- `LocalGraphMemory` is a real knowledge graph with real BM25 retrieval and real multi-hop traversal.
- `LocalActions` really writes `ActionRequest` / `ActionOutcome` rows and really advances their state machine.
- Seed data is synthetic but **statistically realistic** (see §6).

If a number appears on screen or is spoken aloud, it was computed from the database. No exceptions.

### 2.3 Money, time, language

* **Money** is stored in **paise as `int`**, never float. Helpers in `munshiji/money.py`:
  `rupees(paise) -> Decimal`, `fmt_inr(paise) -> "₹18,540"`, `fmt_inr_hi(paise) -> "₹18,540"`.
  Indian digit grouping (lakh/crore) is required: `1234567` paise → `₹12,345.67`; `fmt_inr_short` gives `₹12.3L`.
* **Time** is timezone-aware, always **Asia/Kolkata**. Use `munshiji/clock.py`: `now_ist()`, `today_ist()`,
  `day_bounds_ist(date)`. Never call `datetime.now()` bare. All DB timestamps are stored UTC-aware.
* **"Aaj"/"today"** means the IST calendar day. A business day for a kirana runs 07:00–22:00 IST.
* **Language**: every user-facing string exists in **English and Hindi (Devanagari)**, and the spoken
  register is **Hinglish** (natural code-mixed, how merchants actually talk). Insight/action models carry
  `title_en / title_hi / body_en / body_hi`. Never machine-translate at render time; author both.

### 2.4 Safety and human-in-the-loop

* Tools are split into **read tools** (execute immediately) and **write tools** (`requires_approval=True`).
* A write tool NEVER executes directly. It creates an `ActionRequest` in `PENDING_APPROVAL`, the agent
  asks the merchant out loud, and only an explicit approval transitions it to `APPROVED → EXECUTED`.
* Every action is auditable: who requested, what params, when decided, what result, what outcome.
* Udhaar (credit) reminders use a **tone ladder** — `GENTLE | STANDARD | FIRM` — selected from payment
  history. There is no harsher tier. Language templates are polite in every tier; `FIRM` means clear,
  not threatening. This is a deliberate product-safety choice and must be stated in the docs.
* Rate limits: max 1 reminder per khata entry per 7 days, max 50 outbound messages per merchant per day.
  Enforced in `agent/approval.py`, not in the UI.

### 2.5 Code standards

* Python **3.12**, full type hints, `from __future__ import annotations` at the top of every module.
* Pydantic **v2** for all DTOs (`model_config = ConfigDict(...)`, `field_validator`, not v1 style).
* SQLAlchemy **2.0** style: `Mapped[...]` / `mapped_column(...)`, `select()` not `Query`.
* No bare `except:`. No `print()` — use `munshiji.logging.get_logger(__name__)`.
* Public functions get concise docstrings stating units (paise? days? IST?).
* Pure logic (insights, memory ranking, money, clock) must be **unit-testable without a DB or network**.
* Line length 100. Formatting via `ruff format`.

---

## 3. Repository layout

```
paytm/
├── SPEC.md                     ← this file
├── README.md                   ← how to run, demo script, architecture
├── .env.example
├── docker-compose.yml          ← optional: n8n + the API
├── scripts/                    ← PowerShell + bash task runners
├── data/                       ← sqlite db + generated audio cache (gitignored)
├── workflows/                  ← n8n workflow JSON exports
├── docs/                       ← architecture.md, demo-script.md, ppt-outline.md, api.md
├── frontend/                   ← React + Vite + TypeScript companion screen
└── backend/
    ├── requirements.txt
    ├── pyproject.toml
    └── munshiji/
        ├── main.py             ← FastAPI app factory
        ├── config.py           ← Settings
        ├── logging.py
        ├── clock.py            ← IST helpers
        ├── money.py            ← paise helpers
        ├── errors.py
        ├── cli.py              ← typer: seed / serve / ask / demo
        ├── db/
        │   ├── base.py         ← engine, SessionLocal, Base, init_db()
        │   └── models.py       ← ORM (§4)
        ├── schemas/            ← Pydantic DTOs (§5)
        ├── repositories/       ← data access, one module per aggregate
        ├── seed/               ← synthetic data generator (§6)
        ├── insights/           ← the analytics brain (§7)
        ├── providers/          ← protocols + factory (§2.1)
        ├── integrations/       ← real vendor HTTP clients
        ├── memory/             ← local knowledge graph + GraphRAG-lite (§8)
        ├── agent/              ← tools, registry, loop, approval, prompts (§9)
        └── api/                ← FastAPI routes (§10)
```

---

## 4. Database model (`db/models.py`)

SQLite by default (`data/munshiji.db`), Postgres-compatible. All tables use `id: str` ULID-ish primary
keys generated by `munshiji.ids.new_id(prefix)` → e.g. `"cus_01J8ZK…"`. Timestamps are `DateTime(timezone=True)`.

| Model | Key fields |
|---|---|
| `Merchant` | `id, owner_name, shop_name, category, city, locality, language, phone, soundbox_id, opened_at, monthly_rent_paise, business_hours_start, business_hours_end` |
| `Customer` | `id, merchant_id, name, phone, first_seen_at, last_seen_at, txn_count, total_spend_paise, is_khata_customer, tags(JSON)` |
| `Product` | `id, merchant_id, sku, name, name_hi, category, unit, cost_price_paise, sell_price_paise, stock_qty, reorder_level, is_perishable, shelf_life_days, last_restocked_at` |
| `Transaction` | `id, merchant_id, customer_id(nullable), amount_paise, occurred_at, payment_method, channel, is_return` |
| `TransactionItem` | `id, transaction_id, product_id, qty, unit_price_paise, line_total_paise` |
| `KhataEntry` | `id, merchant_id, customer_id, amount_paise, opened_at, due_at, settled_at, status, reminders_sent, last_reminder_at` |
| `Insight` | `id, merchant_id, kind, severity, title_en, title_hi, body_en, body_hi, metrics(JSON), suggested_tool, suggested_params(JSON), score, status, created_at, expires_at` |
| `ActionRequest` | `id, merchant_id, insight_id(nullable), conversation_id(nullable), tool_name, params(JSON), status, requested_at, decided_at, executed_at, result(JSON), target_count, estimated_impact_paise, provider` |
| `ActionOutcome` | `id, action_id, metric, value_num, value_paise, note, observed_at` |
| `Conversation` | `id, merchant_id, channel, language, started_at, ended_at, summary` |
| `Turn` | `id, conversation_id, seq, role, text, text_display, tool_calls(JSON), audio_path, latency_ms, provider, created_at` |
| `MemoryNode` | `id, merchant_id, kind, key, label, text, attrs(JSON), created_at, updated_at` |
| `MemoryEdge` | `id, merchant_id, src_id, dst_id, rel, weight, attrs(JSON), created_at` |

**Enums** live in `db/enums.py` as `str, Enum`:
`PaymentMethod(UPI|CARD|CASH|SOUNDBOX|WALLET)`, `Channel(SHOP|ONLINE|PHONE)`,
`KhataStatus(OPEN|PARTIAL|SETTLED|WRITTEN_OFF)`,
`InsightKind(COLLECTION_ANOMALY|DORMANT_CUSTOMERS|DEAD_STOCK|STOCKOUT_RISK|EXPIRY_RISK|UDHAAR_OVERDUE|FESTIVAL_PREP|PEAK_HOUR|MARGIN_LEAK|PAYMENT_MIX|NEW_CUSTOMER_DROP)`,
`Severity(INFO|LOW|MEDIUM|HIGH|CRITICAL)`,
`ActionStatus(DRAFT|PENDING_APPROVAL|APPROVED|REJECTED|EXECUTING|EXECUTED|FAILED|EXPIRED)`,
`TurnRole(MERCHANT|MUNSHI|TOOL|SYSTEM)`, `Tone(GENTLE|STANDARD|FIRM)`,
`MemoryKind(MERCHANT|CUSTOMER|PRODUCT|DAY|ACTION|INSIGHT|NOTE|CONVERSATION)`.

---

## 5. Schemas (`schemas/`)

Pydantic v2 DTOs mirroring the ORM plus computed view models. Every API response model here.
Money fields are exposed **twice**: `amount_paise: int` and `amount_display: str` (`"₹18,540"`),
so the frontend never does currency math.

---

## 6. Seed data (`seed/`)

`seed/generator.py::generate(session, profile)` builds a defensible synthetic history.
Default profile: **"Sharma General Store"**, a kirana in Lajpat Nagar, Delhi — 180 days of history
ending *today* (IST), ~40–70 transactions/day.

Realism requirements (these drive the insights, so they matter):
1. **Day-of-week seasonality** — Sat/Sun ~1.35×, Tue lowest ~0.85×.
2. **Intraday curve** — bimodal: 08:00–11:00 morning peak, 17:00–21:00 evening peak.
3. **Month cycle** — salary week (1st–7th) uplift ~1.2×; month-end dip.
4. **Festivals** — `seed/festivals.py` 2026 calendar with per-category uplift; a Diwali/Navratri ramp must
   be visible in the data.
5. **Customer cohorts** — ~220 named customers across Champion/Loyal/Regular/Occasional, each with their own
   *personal visit cadence* (mean + jitter) so per-customer dormancy detection is meaningful. ~35% walk-ins
   with no `customer_id`.
6. **Planted, discoverable signals** (the demo depends on these being true in the data, not hardcoded):
   * ~12–15 previously-regular customers who stopped coming 3–6 weeks ago (→ dormancy insight)
   * 3–4 SKUs with zero sales for 45+ days and real capital locked (→ dead stock)
   * 2–3 fast movers about to stock out before the weekend (→ stockout risk)
   * a perishable over-stocked past its shelf life (→ expiry risk)
   * ~18 open khata entries with a realistic aging spread incl. 4 over 60 days (→ udhaar)
   * a **soft collection dip in the last 7 days** vs the weekday baseline (→ anomaly, the opening line of the demo)
   * one category with slipping margin (→ margin leak)
7. **Deterministic**: seeded RNG (`--seed 20260919`) so every run and every demo is identical.
8. Runs in **< 20 seconds** and is idempotent (`--reset` drops and rebuilds).

---

## 7. Insight engines (`insights/`)

Each engine implements:

```python
class InsightEngine(Protocol):
    kind: InsightKind
    def run(self, ctx: InsightContext) -> list[InsightDraft]: ...
```

`InsightContext` carries `session`, `merchant_id`, `as_of: datetime` (IST-aware) and cached frames.
`registry.py::run_all(ctx)` executes every engine, scores, deduplicates and returns a ranked list;
`score = severity_weight * confidence * log1p(impact_rupees)` with recency decay, clamped 0–100.

**Statistical requirements — do NOT replace with naive thresholds:**

* `sales.py`
  * Weekday baseline from trailing 8 weeks using **median + MAD**; robust z = `0.6745*(x-med)/MAD`.
  * **Partial-day projection**: from the historical intraday cumulative distribution, project today's close
    from collections so far, with a confidence band. (This is what makes the live demo feel alive.)
  * Peak-hour histogram; payment-mix drift (UPI vs cash share, χ²-style flag).
* `customers.py`
  * **RFM** quintile scoring → segments.
  * **Per-customer cadence dormancy**: dormant iff `days_since_last > median_gap + 1.5*IQR` for *that*
    customer (min 3 visits of history), not a global 30-day rule.
  * Win-back value = `P(return|offer) × avg_ticket × expected_visits_in_window`, `P` from segment priors.
* `inventory.py`
  * EWMA consumption rate (α=0.3) from `TransactionItem`.
  * `days_of_cover = stock_qty / max(rate, ε)`; stockout risk if `< lead_time_days + safety`.
  * Dead stock: no sale ≥45d **and** locked capital ≥ ₹500; report capital locked.
  * Expiry risk for perishables: `days_of_cover > remaining_shelf_life`.
  * Restock qty with festival uplift multiplier.
* `credit.py`
  * Aging buckets 0–15/16–30/31–60/60+; reliability score from settle history.
  * `chase_priority = amount × risk_weight × recoverability`; maps to `Tone`.
* `festivals.py` — 2026 calendar + days-until + per-category prep recommendation.

Every `InsightDraft` must carry `metrics` rich enough that the agent can speak the numbers, and a
`suggested_tool` + `suggested_params` so an insight converts to an action in one step.

---

## 8. Memory (`memory/`, Cognee)

Conceptual model: a **knowledge graph over the merchant's world**, queried GraphRAG-style —
lexical/vector retrieval finds seed nodes, then graph traversal resolves the surrounding
entity–relationship neighbourhood.

* `memory/ingest.py` converts DB state into nodes + edges: merchant profile, daily rollups, customer facts,
  product facts, insights, **action outcomes**, conversation summaries, merchant notes.
* `memory/graph.py` — local store over `MemoryNode`/`MemoryEdge` (no external service).
* `memory/retrieval.py` — **BM25** (own implementation, no heavy deps) over node text → top-k seeds →
  1–2 hop expansion with edge-weight decay → assembled context block with provenance.
* `CogneeMemory` (live) maps the same operations onto Cognee's API: `add` → `cognify` → `search`.
  Keep the interface identical so swapping is a config flag.

The **demo-critical** property: an action executed in conversation #1 is ingested as
`(action)-[targeted]->(customer)` and `(action)-[recovered]->(amount)`, so conversation #2 can answer
*"pichli baar jo offer bheja tha uska kya hua?"* purely from memory.

---

## 9. Agent (`agent/`)

* `tools/` — one module per tool group. Each tool is a `Tool` dataclass:
  `name, description, params_model (pydantic), requires_approval, handler, speak_template_hi/en`.
* `registry.py` — `ToolRegistry` with JSON-schema export for the LLM, and `execute(name, params, ctx)`.
* `loop.py` — `AgentLoop.run_turn(conversation_id, text) -> TurnResult` implementing:
  1. recall memory → context block
  2. build prompt (persona + merchant snapshot + memory + tool schemas)
  3. LLM call → tool calls (max 4 iterations, then answer)
  4. read tools execute; write tools create `ActionRequest(PENDING_APPROVAL)` and the reply **asks for confirmation**
  5. approval intent (`haan/bhej do/kar do/yes`) on the next turn resolves the pending action
  6. persist `Turn`, ingest to memory
* `approval.py` — state machine + rate limits (§2.4).
* `prompts.py` — the MunshiJi persona: a respectful, concise Indian bookkeeper who speaks Hinglish,
  always leads with the number, never invents a figure, always proposes exactly one next action,
  and asks before doing anything outbound.

**Tools (v1):**

Read: `get_sales_summary`, `compare_sales`, `get_top_customers`, `find_dormant_customers`,
`get_inventory_alerts`, `get_udhaar_summary`, `get_insights`, `get_product_performance`, `recall_memory`.

Write (approval): `send_winback_offer`, `send_udhaar_reminder`, `create_payment_link`,
`draft_restock_order`, `schedule_followup`, `save_merchant_note`.

---

## 10. API (`api/`)

FastAPI, all routes under `/api`. JSON only, except the audio + WS endpoints.

```
GET  /api/health                     → status of every provider (live/local) — powers the UI badge
GET  /api/merchant/{id}              → profile + today snapshot
GET  /api/merchant/{id}/dashboard    → today's numbers, projection, sparkline, mix
GET  /api/insights/{merchant_id}     → ranked insights
POST /api/insights/{merchant_id}/refresh
POST /api/chat                       → {merchant_id, conversation_id?, text} → TurnResult
WS   /api/voice/stream               → binary audio in → partial transcript / reply / audio out
POST /api/voice/transcribe           → one-shot audio upload → text
POST /api/voice/speak                → text → audio bytes
GET  /api/actions/{merchant_id}      → action feed
POST /api/actions/{id}/approve|reject
GET  /api/memory/{merchant_id}/graph → nodes+edges for visualisation
POST /api/memory/{merchant_id}/search
GET  /api/events/{merchant_id}       → SSE live event stream for the dashboard
```

Every response includes `meta: {provider, latency_ms, as_of}`.

---

## 11. Frontend (`frontend/`)

React 18 + Vite + TypeScript. A **merchant companion screen** designed to be projected next to the
phone during the live demo, so judges can watch the system think.

Panels: live call (waveform, partial transcript, MunshiJi reply, latency + provider badges),
today's numbers (collection, projection, vs-baseline delta, sparkline), ranked insight feed,
action queue with **Approve / Reject**, memory graph visualisation, and a provider status strip.

No component library bloat — hand-rolled CSS, Indian-fintech-appropriate visual language, dark + light.

---

## 12. Configuration (`.env`)

```
MUNSHIJI_ENV=dev
MUNSHIJI_DB_URL=sqlite:///data/munshiji.db
MUNSHIJI_PROVIDER_MODE=auto           # auto | live | local
SARVAM_API_KEY=
SARVAM_STT_MODEL=saaras:v3
SARVAM_LLM_MODEL=sarvam-105b
SARVAM_TTS_MODEL=bulbul:v3
SARVAM_TTS_SPEAKER=anushka
COGNEE_API_KEY=
COGNEE_BASE_URL=https://platform.cognee.ai
N8N_BASE_URL=http://localhost:5678
N8N_WEBHOOK_TOKEN=
MUNSHIJI_DEFAULT_LANGUAGE=hi-IN
MUNSHIJI_SEED=20260919
```

`config.py` exposes a cached `get_settings()`. Missing credentials are **never fatal**.

---

## 13. Testing

`pytest` in `backend/tests/`. Required coverage:
* `test_money.py`, `test_clock.py` — formatting incl. lakh grouping, IST boundaries.
* `test_seed.py` — determinism, planted signals actually present.
* `test_insights_*.py` — each engine against fixtures with a **known correct answer**.
* `test_memory.py` — BM25 ranking, multi-hop expansion, cross-session recall.
* `test_nlu.py`, `test_providers_voice.py` — intent parsing across scripts; no invented numbers.
* `test_messaging.py`, `test_actions.py` — the tone ladder's forbidden vocabulary, idempotency.
* `test_approval.py` — the gate blocks unapproved writes; rate limits; state machine.
* `test_agent_loop.py` — reply register, referent resolution, honesty guard, approval branch.
* `test_api.py` — every route against a seeded temp DB, through the real app.

Target: the full suite runs offline in under 60 seconds. **Achieved: 607 tests, ~59s.**

A test may not assert a wall-clock latency as its primary claim. Timing bounds belong in a
printed benchmark with generous headroom; the assertion should be on the property that causes
the speed (e.g. "a warm query reuses the index"), so the suite cannot fail because a browser
was open.

---

## 14. Definition of done

1. `scripts/setup.ps1` → venv, deps, seed, frontend install — one command, from clean.
2. `scripts/dev.ps1` → API + frontend running.
3. `pytest` green, offline.
4. `munshiji demo` replays the full two-conversation demo in the terminal, no network.
5. `README.md` explains architecture, the three sponsor integrations, and the demo script.
6. Every number shown or spoken traces back to a DB query.
