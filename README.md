> **This repository holds the backend, docs and n8n workflows.**
> The React companion screen lives at
> **[NAME-ASHWANIYADAV/paytm](https://github.com/NAME-ASHWANIYADAV/paytm)**.
>
> ```bash
> cd backend && python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"
> .venv/Scripts/python -m munshiji.cli seed --reset   # 180 days of shop history, ~3s
> .venv/Scripts/python -m munshiji.cli demo           # the full two-conversation demo
> .venv/Scripts/python -m pytest                      # 656 tests, offline, ~75s
> ```
>
> No API keys are required for any of the above.

---

<div align="center">

# MunshiJi · मुंशीजी

**हर merchant का AI मुंशी** — the AI business partner that knows your shop,
advises you, and gets things done. In your language.

*Paytm Build for India AI Hackathon · Delhi Edition 2026 · Track 1, Merchant Growth AI*
*Team Neural Wave*

</div>

---

## The idea

Paytm gave India's merchants the data. Nobody gave them back the **munshi** — the trusted
bookkeeper who kept the khata, knew every customer by name, and told the owner plainly what to do
about it.

A kirana owner will not open a dashboard. He is standing at a counter with his hands full, and he
does not read English. But he will **talk**.

So MunshiJi is a business partner he simply calls:

| | | |
|---|---|---|
| **जानता है** | *It knows the shop* | A knowledge graph over transactions, customers, inventory and udhaar |
| **बताता है** | *It advises* | Statistical insight engines — anomalies, dormancy, dead stock, credit risk |
| **कर देता है** | *It acts* | A tool-calling agent behind a human approval gate, executing through n8n |

The thing that separates it from a chatbot is **memory across sessions**: an action taken in
Monday's call is recalled, with its measured outcome, in Tuesday's.

```
Merchant:  "Munshiji, aaj dhandha kaisa raha?"
MunshiJi:  "आज का गल्ला अभी ₹15,353। पिछले औसत से 2% नीचे। टॉप ग्राहक दिखा दूं?"

Merchant:  "Kaun kaun purane customer nahi aa rahe?"
MunshiJi:  "14 पुराने ग्राहक काफी दिन से नहीं आए — करीब ₹4,333 का नुकसान।
            जैसे Naveen Dua, Manpreet Mishra और Manpreet Dubey।
            इन्हें विनबैक ऑफर भेज दूं?"

Merchant:  "Unhe 10% ka offer bhej do"
MunshiJi:  "10 ग्राहकों के लिए ऑफर तैयार है, 10% की छूट। 4 को अनुमति नहीं थी।
            ₹7.80 खर्च, 1 ग्राहक से निकल जाएगा। भेज दूं?"

Merchant:  "Haan, bhej do"
MunshiJi:  "भेज दिया — 10 ग्राहकों को। कौन लौटा, बताता रहूंगा।"

           … three days later, a brand-new session, empty context window …

Merchant:  "Munshiji, pichli baar jo offer bheja tha uska kya hua?"
MunshiJi:  "पिछली बार: 14 सितंबर 2026 को 10 ग्राहकों को 10% का ऑफ़र भेजा गया।
            उससे ₹2,016.33 वापस आया। फिर वैसा ही कर दूं?"
```

Fourteen regulars had gone quiet; ten could lawfully be contacted, four had never consented to
marketing. Reaching those ten cost **₹7.80** and brought back **₹2,016**.

That last answer is a graph traversal, not a text match — and every rupee in it was computed from
the database.

---

## Run it

**Requirements:** Python 3.12+, Node 20+. **No API keys.** No internet.

```powershell
pwsh scripts/setup.ps1      # venv, deps, 180 days of shop history, frontend install
pwsh scripts/demo.ps1       # the full two-conversation demo, in the terminal
pwsh scripts/dev.ps1        # API on :8000 (docs at /docs), companion screen on :5173
```

Individual commands:

```bash
munshiji seed --reset       # rebuild the demo database (deterministic, ~3s)
munshiji health             # which implementation is serving each capability
munshiji insights           # the ranked feed
munshiji ask "aaj kitna aaya?"
munshiji demo               # the demo replay
```

Tests — offline, in well under a minute:

```bash
cd backend; .venv/Scripts/python.exe -m pytest
```

> **656 tests across 21 files, green, with no network and no credentials.**

---

## The rule this project is built on

Every external capability has **one interface and two implementations**:

```
providers/llm.py     → LLMProvider     → SarvamLLM     | LocalLLM
providers/stt.py     → STTProvider     → SarvamSTT     | LocalSTT
providers/tts.py     → TTSProvider     → SarvamTTS     | LocalTTS
providers/memory.py  → MemoryProvider  → CogneeMemory  | LocalGraphMemory
providers/actions.py → ActionProvider  → N8nActions    | LocalActions
```

`MUNSHIJI_PROVIDER_MODE=auto` (the default) uses a vendor when its key is present **and** a health
probe passes, and otherwise falls back to the offline implementation with a logged warning.
`GET /api/health` reports which is serving, and the companion screen shows it as three chips.

**Why this mattered more than one extra feature.** Venue Wi-Fi dies. Keys expire. Vendors
rate-limit mid-demo. A judging slot is eight minutes long. A system that degrades to a working
offline path is worth more than one that is five percent more impressive and occasionally shows a
stack trace. It also makes every layer testable, which is why the suite runs offline in 41 seconds.

**Local does not mean fake.** `LocalLLM` is a real Indic intent parser (100% on a 75-utterance
table across Devanagari, Hinglish and English) plus a bilingual composer working from real tool
results. `LocalGraphMemory` is a real knowledge graph with hand-rolled BM25 and multi-hop
traversal. `LocalActions` really advances the action state machine and writes real outcome rows.
**Every number spoken or displayed was computed from the database** — there is a test asserting no
unsourced rupee figure can appear in a reply.

---

## Two things that answer the questions a judge actually asks

### "What does this cost, and is it worth it?"

Every proposed action carries its own arithmetic, because that is how a shopkeeper decides:

```
14 ग्राहकों के लिए ऑफर तैयार है, 10% की छूट। ₹10.92 खर्च, 1 ग्राहक से निकल जाएगा।
8 खातों का रिमाइंडर तैयार है, कुल ₹29,785। भेजने का खर्च सिर्फ ₹0.96।
```

The second line is the interesting one. WhatsApp prices a promotional message and a message about
an existing transaction very differently — a win-back offer is *marketing*, a reminder about an
outstanding balance is *utility*, and roughly six times cheaper. So chasing ₹29,785 of udhaar
costs 96 paise, which inverts the usual intuition that collections is the expensive, awkward
thing to automate. MunshiJi knows the difference and says the break-even out loud.

### "Why would Paytm care?"

Paytm does not make money selling dashboards to kirana owners. It makes it on payments, device
subscriptions and **distributing credit** — and the hardest part of lending to a shop with no
audited accounts is knowing whether it is a good shop.

A merchant who talks to MunshiJi every day is, as a side effect, producing exactly that evidence.
`GET /api/merchant/{id}/health` returns an explainable signal built from the same engines:

```
SCORE  84.4/100   band=strong   weakest=revenue_trend
  Revenue trend          64.9    ₹5,57,734 vs ₹5,44,500 (+2.4%)
  Credit discipline      90.7    ₹82,808 open (15% of turnover), 7 past 60 days
  Customer retention     97.0    97% repeat, 0% lapsed
  Inventory efficiency   93.5    ₹5,833 idle of ₹2,32,243
  Digital maturity       84.8    85% of takings are digital
```

Every dimension carries its number and a sentence explaining it, because a score a credit officer
cannot interrogate is one they will not use. **It is a signal, not a credit decision** — nothing
here approves or prices anything.

---

## What it refuses to do

The approval gate asks the merchant before anything goes out. `compliance.py` asks a different
question first — *may this be sent at all?* — and the refusals are real, spoken, and recorded.

| Rule | What it stops | Where it comes from |
|---|---|---|
| **Collection hours** | Reminders outside **8am–7pm IST**. Deliberately narrow: it binds reminders only, because a win-back offer is shop marketing, not debt collection | RBI fair-practices expectations for recovery conduct |
| **Consent** | Promotional messages to anyone who never opted in. On stage the offer visibly drops from **14 recipients to 10** | DPDP Act — purchase history is not a marketing list by default |
| **Opt-out** | Everything, for anyone who asked to be left alone | Both of the above |
| **Template category** | A tool billing a promotional message as cheaper "utility" | WhatsApp policy — it is a breach, not an optimisation |
| **Tone ceiling** | No rung above FIRM. No consequences, no legal language, no shaming — enforced by a forbidden-terms guard | The merchant has to sell this person groceries again tomorrow morning |

A guardrail nobody can hear is decoration, so the refusal is spoken in the merchant's language:

```
Merchant:  "Udhaar wale customers ko reminder bhej do."     (at 21:58)
MunshiJi:  "Yaad dilane ka sandesh subah 8 se shaam 7 ke beech hi jaata hai."
```

---

## Where the three sponsors sit

| | Role | Why it is load-bearing |
|---|---|---|
| **Sarvam AI** | `saaras:v3` speech-to-text → `sarvam-105b` tool-calling reasoning → `bulbul:v3` speech | The product *is* a conversation in Hindi. The user does not read English dashboards. Remove Sarvam and there is no product, only a dashboard. |
| **n8n** | Every approved action becomes a webhook into a workflow that fans out to WhatsApp and reports delivery back | This is the "कर देता है" half. Without it MunshiJi advises and stops — which is the chatbot we set out not to build. |
| **Cognee** | Long-term memory as a knowledge graph, queried GraphRAG-style | The cross-session recall above. Plain RAG returns text that looked similar; the graph returns `action -targeted-> customer` and `action -recovered-> ₹1,656`. |

Every integration point is **vendor-documented leg by leg**. We do not claim an official
end-to-end reference stack, because there isn't one — we assembled it. See
[`workflows/`](workflows/README.md) for the n8n side, including a Cognee **Add Data → Cognify →
Search** pipeline that asserts its own read-back.

---

## What's actually in here

```
backend/munshiji/
├── clock.py money.py ids.py    IST everywhere; money is int paise, never float
├── economics.py                what an action costs, and its break-even
├── compliance.py               who may be contacted, and when
├── messaging.py                the actual words, hand-written in both languages
├── db/                         13 tables; UtcDateTime keeps timestamps aware through SQLite
├── seed/                       180 days of statistically realistic kirana history
├── insights/                   the analytics brain (robust statistics, no numpy)
├── memory/                     knowledge graph + BM25 + multi-hop retrieval
├── providers/                  one protocol, two implementations, per capability
├── agent/                      tools, registry, the loop, and the approval gate
└── api/                        FastAPI + SSE
frontend/                       the companion screen (React, no UI framework)
workflows/                      n8n: outbound actions, and Cognee memory sync
docs/                           architecture · demo script · PPT outline
```

### The seed data has to be found, not read

`munshiji seed` writes 180 days for *Sharma General Store*, Lajpat Nagar — weekday seasonality
(Sat ≈1.35×, Tue lowest), a bimodal intraday curve, salary-week uplift, festival ramps, ~220
customers **each with their own visit cadence**, and a payment mix drifting toward UPI. Into that
it plants signals the engines then have to discover:

```
transactions  9,791 (54/day, 35% walk-ins)      items 27,176
collection    ₹32,23,444  (avg bill ₹329)
gross margin  11.9% blended — dairy 4.7%, staples 5.5% … spices 21.2%, personal care 20.1%
dormant       13 regulars stopped 22–40 days ago
dead stock    4 SKUs, ₹5,833 locked
stockout      3 SKUs at 1.9–2.8 days of cover
expiry        2 perishables whose cover outlasts their shelf life
udhaar        34 open entries, ₹82,808 — 15% of monthly turnover, 7 over 60 days
consent       78% marketing-consented, 2% opted out — so the screen has someone to refuse
collection    last 7d −12.3% against the weekday baseline
margin leak   dairy 4.9% → 1.4% over 30 days
```

Deterministic under a seed, so every demo is identical. ~2.4 seconds.

The margin *spread* is the part that matters. A kirana cannot set its own prices — the pack
carries an MRP — so margin is whatever the distributor's trade margin leaves, and it varies
enormously by category: staples and dairy are the traffic drivers a shop sells almost at cost,
while spices and personal care are where it actually earns. Model that flat and the margin-leak
engine has nothing to find and "push the profitable category" stops being advice. The credit book
is sized the same way: at a few percent of turnover, udhaar looks like loose change instead of
the working-capital problem it is.

### The statistics are the point

* **Weekday baseline** is a **median + MAD** over the trailing eight *same weekdays*, flagged on a
  robust z-score. Mean-and-SD would be dragged around by one festival Saturday.
* **Partial-day projection** builds today's close from this shop's own historical intraday
  cumulative curve, with a confidence band, and *refuses to project* before 8% of the trading day
  has elapsed rather than amplify noise by 1/share.
* **Dormancy is per customer** — dormant means late *by that customer's own rhythm*
  (`days_since_last > median_gap + 1.5 × IQR`), not a global 30-day rule. A global rule flags the
  daily shopper who skipped a weekend and misses the monthly regular who has vanished. This one
  choice is the difference between a useful win-back list and a spam list.

---

## Safety, on purpose

1. **Nothing outbound without an explicit yes.** Not a setting — a typed state machine. A write
   tool can only *propose*; `execute_action` refuses anything not in `APPROVED`, and a test proves
   the provider is never even reached.
2. **Reminders stop at a `FIRM` register** — clear and direct, never threatening, never mentioning
   consequences or shaming. Collections software is where fintech causes real harm; we picked the
   ceiling deliberately and test against a forbidden-vocabulary list. A reliable payer can never
   reach `FIRM` at any age or amount.
3. **Rate limits live in the domain layer**, not the UI: one reminder per khata entry per week, a
   daily outbound cap per merchant.
4. **Unanswered proposals expire** rather than firing hours later.
5. **Full audit trail**: who proposed, what, when decided, what result, what measured outcome.

---

## Known limits

Stated plainly, because a judge will find them anyway:

* Single merchant, single process. SQLite and an in-memory event bus — both correct at demo scale,
  both behind interfaces that would swap for Postgres and Redis.
* Local LLM mode is an intent router, not a general reasoner. It handles the merchant-copilot
  intent space well and says so when it doesn't understand, rather than guessing.
* Vendor capability claims come from vendor documentation. Real ASR accuracy on a noisy demo floor
  is unverified — which is exactly why the offline path exists.
* Outcome simulation (redemptions arriving after an offer) is derived from real customer segments
  and real historical ticket sizes, but it *is* a simulation, and the UI says so.

---

## Docs

* [`docs/architecture.md`](docs/architecture.md) — how it fits together, and why
* [`docs/demo-script.md`](docs/demo-script.md) — the words to say on stage, and the failure drill
* [`docs/ppt-outline.md`](docs/ppt-outline.md) — the submission deck, slide by slide
* [`SPEC.md`](SPEC.md) — the engineering contract every module was written against
* [`workflows/README.md`](workflows/README.md) — the n8n payload contract
