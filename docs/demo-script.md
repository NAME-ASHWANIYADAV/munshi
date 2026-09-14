# MunshiJi — Demo Script

**Total: 4 minutes of demo, 1 minute of architecture, leave 3 for questions.**
Rehearse this twice before the slot. The exact words matter less than the *order* — the order is
what makes the memory reveal land.

---

## Before you walk in (10-minute checklist)

| Check | Command / action |
|---|---|
| Database seeded and fresh | `pwsh scripts/setup.ps1` then confirm the dashboard loads |
| Offline path proven | Turn Wi-Fi **off**, run `pwsh scripts/demo.ps1` — it must complete |
| Live path proven | Wi-Fi on, keys in `.env`, check `/api/health` shows `sarvam/cognee/n8n: live` |
| Backup video recorded | Full two-call flow, screen + audio, on the laptop *and* on a phone |
| Companion screen zoomed | Browser at 110–125%; the provider strip must be readable from 3 metres |
| Phone on speaker, volume up | Test the actual room if you can |
| Terminal fallback ready | A second tab already at `backend/`, ready to run `demo.ps1` |

**If anything breaks on stage, say this out loud and move:** *"Wi-Fi's gone — MunshiJi is built to
survive exactly this, so it's now running fully offline."* Then run the terminal demo. This is a
strength, not an excuse. Practise saying it.

---

## The 30-second framing (before you touch anything)

> "Paytm gave forty crore merchants the data. Nobody gave them back the **munshi** — the bookkeeper
> who kept the khata, knew every customer, and told the owner what to do about it.
>
> A kirana owner will not open a dashboard. He is standing at a counter, and he does not read
> English. But he will *talk*.
>
> So we built MunshiJi: an AI munshi he simply calls. It knows his shop, it advises him, and with
> his permission, it gets things done."

Hold up the phone. Do not read the slide.

---

## Call 1 — "Aaj dhandha kaisa raha?" (90 seconds)

**You (into the phone, in Hindi):** *"Munshiji, aaj dhandha kaisa raha?"*

**What the audience should see on the companion screen while it answers:**
- the waveform moving (real `AnalyserNode`, not a loop)
- the tool trace lighting up: `get_sales_summary` → `get_insights`
- the provider chips: Sarvam / Cognee / n8n

**MunshiJi replies** (numbers come from the seeded database, so they will be exact on the day):

> *"Abhi tak ₹11,240 aaye hain. Is hisaab se din ₹18,500 ke aas-paas band hoga — pichhle Mangalvaar
> se karib 12% kam. Ek wajah dikh rahi hai: aapke 12 purane regular customer is mahine ek baar bhi
> nahi aaye."*

**Point at the screen and say the one line that wins the technical argument:**

> "Two things there that a chatbot cannot do. That projection is built from this shop's own
> intraday curve, not a guess. And 'dormant' is measured **per customer** — someone who comes every
> three days and is four days late is fine; someone who comes every month and is sixty days late is
> not. A thirty-day rule gets both of those backwards."

---

## Call 1 continued — the approval (45 seconds)

**MunshiJi asks:**
> *"Un 12 customers ko ₹50 ka win-back offer bhejoon? Andaaza ₹8,400 wapas aa sakta hai."*

**Do not say yes immediately.** Point at the action card sitting in **PENDING** on screen.

> "Notice it stopped. MunshiJi does not send anything on its own. This isn't a setting we could
> forget to switch on — it's a state machine. An unapproved action physically cannot reach the
> execution layer, and we have a test that proves it."

**Then say:** *"Haan, bhej do."*

Screen: the card moves to **EXECUTED**, the n8n chip pulses, delivery counts appear.

> "That went out through n8n — one webhook, fanning out to WhatsApp. And the outcome is now written
> back into the knowledge graph, which is the part that matters next."

---

## Call 2 — the kill shot (60 seconds)

Say this to the room first, so they know what to watch for:

> "Now the thing every hackathon demo skips. I'm going to hang up, and call back as if it's the
> next day."

Hang up. **Start a new conversation** (new session — make this visible on screen).

**You:** *"Munshiji, pichli baar jo offer bheja tha, uska kya hua?"*

**MunshiJi:**
> *"12 customers ko bheja tha. Unme se 4 wapas aaye aur ₹2,340 ka saman le gaye. Baaki 8 ko ek aur
> yaad dilaana chahiye?"*

**Land it:**

> "New session. Nothing in the context window. That answer came out of the Cognee knowledge graph —
> the *action* node, its edges to the twelve customers it targeted, and the outcome measured
> against their real purchases. Plain vector search would have returned text that looked similar.
> The graph returned what actually happened.
>
> That is the difference between a chatbot and a teammate: a teammate remembers what it did for
> you, and tells you whether it worked."

---

## The refusal (30 seconds) — optional, and worth it with a fintech panel

Only if the clock allows, and best if a judge has already asked something sceptical about safety.

**You:** *"Udhaar wale customers ko abhi reminder bhej do."*

If it is after 7pm, MunshiJi refuses out loud:

> *"Yaad dilane ka sandesh subah 8 se shaam 7 ke beech hi jaata hai."*

**Land it:**

> "Chasing money owed is collection contact. It keeps to daylight hours, it never goes to someone
> who asked to be left alone, and there is no tone above 'firm' — no consequences, no legal
> language, no shaming. The merchant has to sell this person groceries again tomorrow morning.
>
> And notice the offer earlier went to ten people, not fourteen. Four of those regulars never gave
> consent to be marketed to, so it didn't send. A guardrail nobody can hear is decoration — this
> one says why, in the merchant's own language."

*(During the day the reminder path runs normally, so lead with the consent drop instead: point at
the action card showing **10 target** where the insight found fourteen.)*

---

## If a judge asks "why would Paytm build this?" (30 seconds)

Point at the header chip — it has been on screen the whole time.

> **84 · dukaan ki sehat · मजबूत −2.0**

> "Paytm doesn't make money selling dashboards to kirana owners. It makes it on payments, device
> subscriptions and distributing credit. The hardest part of lending to a shop with no audited
> accounts is knowing whether it's a good shop.
>
> A merchant who talks to MunshiJi every day is producing exactly that evidence as a side effect.
> This score is the same five engines read as an underwriting signal — revenue trend, how
> disciplined he is with his own credit book, whether customers come back, whether stock turns, how
> much of the business is digitally visible at all. Every dimension carries its number and a
> sentence, because a score a credit officer can't interrogate is one they won't use.
>
> It's a signal, not a credit decision. But it's the reason this is a Paytm product and not a
> feature."

---

## The architecture minute (60 seconds)

Switch to the architecture slide. Point at three boxes, one sentence each:

1. **Sarvam** — *"The whole conversation. `saaras:v3` for code-mixed Hindi in, `sarvam-105b`
   reasoning with tool calls, `bulbul:v3` streaming back out. The user doesn't read English; remove
   this and there is no product."*
2. **n8n** — *"Every approved action is a webhook into a workflow. This is the 'gets it done' half."*
3. **Cognee** — *"Long-term memory as a knowledge graph, which is what you just watched."*

Then the line that separates you from every other team:

> "Every one of those has a second implementation behind the same interface that runs fully
> offline. Not stubs — a real intent parser, a real BM25 graph search, a real action state machine.
> We did that because a judging slot is eight minutes and venue Wi-Fi is venue Wi-Fi. You can pull
> the network out of this laptop right now and the demo still runs."

Offer it. If they take you up on it, you win the room.

---

## Answers to the questions you will actually get

**"Is any of this hardcoded for the demo?"**
> "No. The seed generator writes 180 days of statistically realistic history — weekday seasonality,
> festival ramps, per-customer visit cadences — and the engines have to *find* the signals in it.
> Every number spoken was computed from the database. There's a test asserting no unsourced rupee
> figure ever appears in a reply."

**"How is this different from the AI Soundbox Paytm already ships?"**
> "The Soundbox answers questions about payments. MunshiJi is the layer above it: it decides what
> is worth the merchant's attention, proposes the action, executes it across systems, and measures
> whether it worked. Mr. Sharma said at GFF that Paytm is building an SLM that acts as a CFO for
> merchants. This is that, demoed — and it would sit on top of the Soundbox, not replace it."

**"What happens when the LLM hallucinates a number?"**
> "It can't say a number that isn't in a tool result — that's a prompt rule *and* a test. And no
> number it says can trigger an action: actions are a separate, typed path through an approval gate
> with rate limits."

**"How would this scale to millions of merchants?"**
> "The per-merchant work is a few hundred rows and a small graph — it's cheap. The honest limits
> today are single-process: SQLite and an in-memory event bus, both behind interfaces that swap for
> Postgres and Redis. I'd rather tell you that than pretend."

**"Why not just use WhatsApp?"**
> "We do, for outbound — through n8n. But inbound has to be voice: the merchant is standing at a
> counter with his hands full, and typing Devanagari is slow. Voice is the accessibility argument
> for this user, not a gimmick."

---

## What to do if you have 90 seconds, not 4

Cut to: framing (15s) → Call 1 up to the approval (40s) → Call 2 memory reveal (25s) → the
"pull the network out" line (10s). Skip the architecture slide; they will ask.
