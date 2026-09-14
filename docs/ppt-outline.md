# MunshiJi — Submission Deck (10 slides)

Team **Neural Wave** · Track 1, Merchant Growth AI · Paytm Build for India AI Hackathon, Delhi 2026

> Shortlisting runs on one PPT round, so this deck is the gate. Rules of thumb: one idea per slide,
> the number before the adjective, and a screenshot beats a diagram beats a paragraph. Export to PDF
> before submitting so fonts don't reflow.

---

### 1 — Title
**MunshiJi — har merchant ka AI munshi**
*The AI business partner that knows your shop, advises you, and gets things done — in your language.*
Team Neural Wave · Track 1 · one screenshot of the live call in progress.

### 2 — The problem, from the counter
Merchants are **data-rich and insight-poor**. Three specific pains, not a lament:
- Regulars quietly stop coming and nobody notices until the month is over.
- Udhaar sits in a paper khata; chasing it is awkward, so it doesn't happen.
- Restocking is guesswork; capital sits dead on the shelf.

The framing line: *a kirana owner will not open a dashboard — he is at a counter, and he doesn't read English. But he will talk.*

### 3 — Meet MunshiJi
Three capabilities, in Hindi, in ascending order of difficulty:
**Jaanta hai** (knows the shop) · **Batata hai** (advises) · **Kar deta hai** (acts, with permission).
One screenshot. No feature list.

### 4 — The demo journey
Storyboard, four panels: question → insight → **approval** → remembered outcome.
Panel 4 is the point: *the second call knows what the first call did, and what it earned.*
Caption: "New session. No context window. That came out of the knowledge graph."

### 5 — Architecture
The three-sponsor diagram with exact model names — `saaras:v3` → `sarvam-105b` → `bulbul:v3`,
n8n webhooks + approval gate, `n8n-nodes-cognee` graph memory.
The claim to make, precisely: **"every integration point is vendor-documented."** (Not "official
end-to-end stack" — there isn't one, and a judge may know that.)

### 6 — Why this AI stack *(your Best-AI-Usage slide)*
- **Sarvam** — the entire conversation. The user doesn't read English; remove it and there's no product.
- **Cognee** — graph memory, not plain RAG: vector similarity finds seeds, graph traversal returns
  the relationships (`action -targeted-> customer`, `action -recovered-> ₹2,340`).
- **n8n** — the action layer with the human approval gate and delivery reporting.
Close with the engineering line: *each has a fully functional offline implementation behind the
same interface, so the demo cannot fail.*

### 7 — Why Paytm, why now
- Vijay Shekhar Sharma, at the AI Soundbox launch: **"entering the intelligence age with multiple AI first products."**
- At GFF he described an SLM acting as a **CFO-like advisor** for merchants, in their own language.
- The AI Soundbox already talks to merchants in **11 languages**.
**MunshiJi is the agentic layer on top of what Paytm already ships** — it extends the Soundbox, it
doesn't duplicate it. (One line acknowledging Paytm Pi shows you track them to the week.)

### 8 — Impact & economics
Lead with the line that reframes the whole category, straight from the product:

> **"8 खातों का रिमाइंडर तैयार है, कुल ₹29,785। भेजने का खर्च सिर्फ ₹0.96।"**

WhatsApp prices a promotional message and a message about an existing transaction differently — an
offer is *marketing*, a balance reminder is *utility*, roughly six times cheaper. So chasing
₹29,785 of udhaar costs 96 paise. MunshiJi knows the difference, and every proposal states its cost
and its break-even ("₹10.92 खर्च, 1 ग्राहक से निकल जाएगा") rather than only its upside.

Then the levers: win-back of dormant regulars, faster udhaar settlement, better restock timing.
Directional and honest beats precise and invented — say which numbers are modelled.

**And the second output.** Screenshot `GET /api/merchant/{id}/health`: an explainable
merchant-health score (84/100, weakest dimension named, every dimension carrying its evidence)
falling out of the same engines. Paytm's money is in payments, subscriptions and **distributing
credit**, and the hardest part of lending to a shop with no audited accounts is knowing whether it
is a good shop. A merchant who talks to MunshiJi daily produces that evidence as a by-product. Say
plainly: **a signal, not a credit decision.**

### 9 — Buildable in 8 hours
The hour-by-hour plan, compressed to one table, plus the safety story — and make this concrete,
because in fintech "we thought about what goes wrong" is a credibility marker:
- **approval gate** on every outbound action, with an audit trail
- **tone ceiling** on reminders — no rung above FIRM, no consequences, no shaming (enforced by a
  forbidden-terms guard and asserted in tests)
- **consent** — an offer is marketing and needs consent on file; a reminder about someone's own
  balance is gated on opt-out instead. On stage the offer visibly drops from 14 recipients to 10.
- **collection hours** — reminders refuse outside 8am–7pm IST, and the refusal is *spoken*:
  "Yaad dilane ka sandesh subah 8 se shaam 7 ke beech hi jaata hai."
- rate limits, cooldowns, and no invented numbers

A guardrail nobody can hear is decoration. Show MunshiJi declining something.

### 10 — Team Neural Wave
Who owns which half of the system (**judges are screening individuals** — make both halves visibly
deep), what is already working today, a 90-day roadmap, and the ask.

---

## Design notes
- Hindi in Devanagari with a real font (Noto Sans Devanagari) — fallback rendering is the fastest
  way to look unfinished.
- Dark slides, one accent colour, tabular figures. Don't use Paytm's logo or brand blue as if it
  were yours.
- Screenshots of the running product on **every** slide that can take one. This deck's job is to
  prove the thing exists.
