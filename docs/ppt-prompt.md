# Prompt for Claude Design — MunshiJi submission deck

Paste everything below the line into Claude Design. It carries the real data on purpose: a deck
reads as machine-written when it is *vague*, not when it is well designed, so every figure here is
the actual output of the built system rather than a placeholder.

---

Build a 12-slide submission deck for a hackathon. It is going to a shortlisting panel at Paytm —
product managers and engineers who know Indian retail intimately and will be reading perhaps forty
of these in a sitting. The deck's single job is to make them say "get these two in the room."

Write it in **English**. The one exception: when the product speaks, quote its actual Hindi output
in Devanagari and put a short English gloss underneath in a smaller, lighter style. Do not
translate those lines away — the product's whole thesis is that it talks to a shopkeeper in his own
language, and a deck that renders it in English quietly argues the opposite.

## The project

**MunshiJi (मुंशीजी)** — a voice-first AI business partner for Indian kirana merchants. Team
**Neural Wave** (2 people). Paytm Build for India AI Hackathon, Delhi Edition, 19 September 2026.
Track 1: Merchant Growth AI.

The *munshi* was the trusted bookkeeper of the Indian merchant — kept the khata, knew every
customer by name, told the owner plainly what to do about it. Paytm gave merchants the data;
nobody gave them back the munshi. A kirana owner will not open a dashboard: he is at a counter with
his hands full and he does not read English. But he will talk.

Three things it does, and the deck should use these three Hindi verbs as a spine:

- **जानता है** — *it knows the shop.* A knowledge graph over transactions, customers, inventory, udhaar.
- **बताता है** — *it advises.* Statistical engines: robust weekday baselines, per-customer dormancy, EWMA inventory, khata aging.
- **कर देता है** — *it acts.* A tool-calling agent behind a human approval gate, executing through n8n.

## Slides

Vary the density deliberately. Slides 4, 5 and 7 should breathe — big type, one idea. Slides 9 and
10 can be dense. Never the same layout twice in a row.

**1 — Title.** MunshiJi · मुंशीजी. "हर merchant का AI मुंशी." Team Neural Wave. Track 1. One line
underneath: *The AI business partner that knows your shop, advises you, and gets things done — in
your language.* Put the two live URLs in small type at the bottom, because a working link on slide
one is a claim most decks cannot make: `paytm-nu-seven.vercel.app` and
`munshiji-api-pr8v.onrender.com`.

**2 — The problem, from behind the counter.** Not "merchants lack insights." Three specific,
uncomfortable facts about a real shop: fourteen regulars have quietly stopped coming and nobody
noticed; ₹82,807 is sitting in the khata book, 15% of monthly turnover, seven accounts past sixty
days; the dairy margin has fallen from 4.9% to 1.4% over thirty days and the owner has no way to
see it. End with the line that frames everything: *he is data-rich and insight-poor, in a language
the data does not speak.*

**3 — What MunshiJi is.** The three verbs, large, with one line each. No feature list. One
screenshot of the companion screen mid-call.

**4 — The conversation.** This is the hero slide and it should feel like a transcript, not a
diagram. Set it as an actual dialogue with generous space. Use these exact lines:

> **"Munshiji, aaj dhandha kaisa raha?"**
> आज का गल्ला अभी ₹15,353। पिछले औसत से 2% नीचे।
> *Today's takings are ₹15,353 so far — 2% below the usual.*

> **"Kaun kaun purane customer nahi aa rahe?"**
> 14 पुराने ग्राहक काफी दिन से नहीं आए — करीब ₹4,333 का नुकसान। जैसे Naveen Dua, Manpreet Mishra और Manpreet Dubey।
> *14 regulars have gone quiet — about ₹4,333 at risk.*

> **"Unhe 10% ka offer bhej do"**
> 10 ग्राहकों के लिए ऑफर तैयार है, 10% की छूट। **4 को अनुमति नहीं थी। ₹7.80 खर्च, 1 ग्राहक से निकल जाएगा।** भेज दूं?
> *Ready for 10 customers. 4 had not consented. ₹7.80 to send; one returning customer covers it.*

Pull out the bolded sentence — it is doing three jobs at once (consent, cost, break-even) and a
reader who notices that is a reader who is impressed.

**5 — The second call.** Same dialogue treatment, but make the slide visibly emptier — this is the
one that should land in silence.

> *Three days later. A new session. An empty context window.*
>
> **"Munshiji, pichli baar jo offer bheja tha uska kya hua?"**
> पिछली बार: 14 सितंबर 2026 को 10 ग्राहकों को 10% का ऑफ़र भेजा गया। उससे ₹2,016.33 वापस आया। फिर वैसा ही कर दूं?
> *Last time: an offer went to 10 customers on 14 September. ₹2,016.33 came back. Shall I do it again?*

One line underneath, no more: *Nothing in the context window. That answer is a graph traversal —
the action node, its edges to the ten customers it targeted, and the outcome measured against their
real purchases. A chatbot answers questions. A teammate remembers what it did for you.*

**6 — ₹7.80 → ₹2,016.** A single-figure slide. Reaching those ten customers cost ₹7.80. ₹2,016.33
came back. Then the insight underneath, which is the non-obvious part: WhatsApp bills a promotional
message and a message about an existing transaction differently. A win-back offer is *marketing*. A
reminder about an outstanding balance is *utility* — roughly six times cheaper, because it concerns
a transaction the customer already entered into. So chasing **₹29,785 of udhaar costs 96 paise.**
That inverts the usual intuition that collections is the expensive, awkward thing to automate.
MunshiJi knows the difference and says the break-even out loud before it spends anything.

**7 — What it refuses to do.** Fintech judges reward this and almost nobody builds it. A short
table — rule, what it stops, where it comes from:

| Collection hours | Reminders outside 8am–7pm IST | RBI fair-practices expectations on recovery conduct |
| Consent | Promotional messages to anyone who never opted in | DPDP Act — purchase history is not a marketing list |
| Opt-out | Everything, for anyone who asked to be left alone | Both of the above |
| Template category | Billing a promotional message as cheaper "utility" | WhatsApp policy — a breach, not an optimisation |
| Tone ceiling | Any rung above FIRM: no consequences, no legal language, no shaming | The merchant has to sell this person groceries again tomorrow morning |

Note that the hours rule deliberately binds reminders only — a win-back offer is shop marketing,
not debt collection, and applying a recovery rule to everything would look thorough and be wrong.
Close with the refusal, spoken, because a guardrail nobody can hear is decoration:

> **"Udhaar wale customers ko reminder bhej do."** *(at 21:58)*
> यह सन्देश सुबह 8 से शाम 7 के बीच ही जाता है।
> *Reminders only go out between 8am and 7pm.*

**8 — Why Paytm, and not just any merchant app.** The strategic slide. Paytm does not make money
selling dashboards to kirana owners — it makes it on payments, device subscriptions and
**distributing credit**. The hardest part of lending to a shop with no audited accounts is knowing
whether it is a good shop. A merchant who talks to MunshiJi every day produces exactly that
evidence as a by-product. Show the real output of `GET /api/merchant/{id}/health`:

```
SCORE  84.4 / 100        band: strong        weakest: revenue trend

Revenue trend          64.9   ₹5,57,734 vs ₹5,44,500 (+2.4%)          weight 0.28
Credit discipline      90.7   ₹82,808 open (15% of turnover), 7 past 60 days   0.26
Customer retention     97.0   97% repeat, 4% lapsed                            0.20
Inventory efficiency   93.5   ₹5,833 idle of ₹2,32,243                         0.14
Digital maturity       84.8   85% of takings are digital                       0.12
```

Every dimension carries its number and a sentence, because a score a credit officer cannot
interrogate is one they will not use. State plainly: **a signal, not a credit decision.**

**9 — Architecture.** Three boxes, one sentence each, with the exact model names — vague
architecture slides are the clearest tell that something was not actually built.

- **Sarvam** — `saaras:v3` speech-to-text → `sarvam-105b` tool-calling reasoning → `bulbul:v3` streaming speech. The product *is* a conversation in Hindi. Remove Sarvam and there is no product, only a dashboard.
- **n8n** — every approved action becomes a webhook into a workflow that fans out to WhatsApp and reports delivery back. This is the कर देता है half.
- **Cognee** — long-term memory as a knowledge graph, queried GraphRAG-style. Vector similarity finds the seed nodes; graph traversal returns `action —targeted→ customer` and `action —recovered→ ₹2,016`.

Add the honest qualifier, which is also a credibility signal: *every integration point is
vendor-documented leg by leg. We do not claim an official end-to-end reference stack, because there
isn't one — we assembled it.*

**10 — It is built, and it does not need the internet.** The engineering slide.

Every external capability has one interface and two implementations — Sarvam / Cognee / n8n when a
key is present and a health probe passes, fully functional local implementations otherwise. Not
stubs: a real Indic intent parser (100% on a 75-utterance table across Devanagari, Hinglish and
English), a real knowledge graph with BM25 and multi-hop traversal, a real action state machine
writing real outcome rows.

**656 tests, green, with no network and no credentials.** Because venue Wi-Fi dies, keys expire,
vendors rate-limit mid-demo, and a judging slot is eight minutes long. Offer it: *pull the network
out of this laptop and the demo still runs.*

Also mention the data, because the realism is load-bearing: 180 days of Sharma General Store,
Lajpat Nagar — 9,791 transactions, 220 customers, 75 SKUs, ₹32,23,444 collected, weekday
seasonality, salary-week uplift, festival ramps, a payment mix drifting 58% → 70% UPI, and category
margins spread from 4.7% on dairy to 21.2% on spices. Deterministic under a seed, so the demo is
identical every time.

**11 — Why now.** Paytm's own words, dated:

- Vijay Shekhar Sharma, AI Soundbox launch, October 2025: **"entering the intelligence age with multiple AI first products."**
- At Global Fintech Fest he described a small language model acting as a **CFO-like advisor** for merchants, answering in their own language.
- The AI Soundbox already talks to merchants in **11 languages**.
- September 2026: Paytm launched **Pi**, an agentic-AI platform for banks and insurers.

The claim: **MunshiJi is the agentic layer on top of what Paytm already ships.** It extends the
Soundbox rather than duplicating it, and it is the merchant SLM VSS described, demoed.

**12 — Team, and what is actually done.** Two people, and say who owns which half — the panel is
screening individuals, not just teams. What is live today (both URLs, both repos, 656 tests). Then
a 90-day line and the ask. Include one honest limitation: the sponsor integrations are
vendor-documented leg by leg and tested against local implementations, not yet against production
Sarvam and Cognee accounts under load. Admitting one real thing is worth more than a slide of
claims.

## Design

Carry the product's own identity, because the deck and the thing should look like they came from
the same hands. The visual world is the **bahi khata** — the Indian ledger: warm paper, ink, a red
margin rule down the left edge, gold for money that came back.

**Palette** (these exact values, taken from the running product):

- paper `#EFE7D9` · panel `#FBF7EE` · rule `#E3D8C4`
- ink `#1A1712` · secondary ink `#4E463A` · faint ink `#7D7263`
- red `#B03528` — the margin rule, and anything that needs attention
- gold `#A8761B` — money recovered, and approvals. **Nothing else.**

Spend the boldness on the red rule and the gold figures; keep everything else quiet.

**Type.** A ledger serif for display (Fraunces, or similar with real weight contrast) against
IBM Plex Sans for body, IBM Plex Mono for figures and code, and IBM Plex Sans Devanagari for the
Hindi — use a real Devanagari face, because fallback rendering is the fastest way to look
unfinished. Money and scores in tabular figures, always.

**The recurring structural device** is the khata's red margin rule down the left edge of every
slide, with the slide's one-word section marker set vertically in it. That is the signature. It is
true to the subject and it is not a decoration bar.

## What would make this read as machine-written — avoid all of it

- Three bullets on every slide. Vary it: some slides are one sentence, some are a table.
- Rounding the numbers. ₹82,807.71 and 96 paise and 4.9% → 1.4% are exactly right as they are; ₹83,000 and "under a rupee" and "a sharp decline" are not.
- Benefit language with no object — "empowering merchants," "driving growth," "seamless experience," "revolutionising." Say what happens instead.
- An icon beside every line.
- Gradients, especially purple-to-blue. There are none in this palette.
- Perfectly parallel phrasing across every heading.
- A generic stock photograph of a shopkeeper. Use screenshots of the actual running product, and leave clearly-marked placeholders for them.
- Claiming without a number attached.
- The same layout twice in a row.

Prefer the things a person does: an uneven rhythm, a slide that is mostly white space, one figure
large enough to be uncomfortable, a real customer's name, a dated quote, an admitted limitation,
and a sentence somewhere that has an opinion in it.

## Screenshot placeholders to leave

Mark clear slots for: the companion screen mid-call (slide 3), the action card showing *10 target ·
₹1,218.43 anumaanit asar · ₹7.80 bhejne ka kharch* (slide 6), the memory graph lit up after a
recall (slide 5), and the health score panel (slide 8).
