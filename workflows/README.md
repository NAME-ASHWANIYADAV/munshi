# n8n workflows

Five importable workflows. MunshiJi runs perfectly without them — the offline `LocalActions`
provider implements the same contract in-process — so bring these up when you want the real
orchestration leg live (SPEC.md §2.1).

| File | What it does |
|---|---|
| `munshiji-actions.json` | Receives every approved action and fans it out to WhatsApp, reporting delivery back |
| `munshiji-memory-sync.json` | Pushes the shop's actions and findings into Cognee: **Add Data → Cognify → Search** |
| `munshiji-digest.json` | **Shaam ka Hisaab** — 19:00 IST digest to the merchant's own WhatsApp, with one-tap approval of the pending action |
| `munshiji-heartbeat.json` | Pings `/api/health` every 5 minutes so the free Render dyno never cold-starts mid-demo |
| `munshiji-evals.json` | **Report Card** — runs `munshiji-evals-dataset.csv` through `/api/chat` and records four metrics per row |

Both are built for **n8n Cloud**: no community nodes, no `$env`. They import and run on a stock
Cloud workspace, and they work self-hosted too — see the note at the end.

---

## Bring up n8n

1. **Workflows → Import from File** → import both JSON files from this folder.

2. **Settings → Variables** → add what the nodes read. (`$vars` is included in Cloud Pro.)

| Variable | Used by | Value |
|---|---|---|
| `MUNSHIJI_TOKEN` | actions | Any long random string. Must equal MunshiJi's `N8N_WEBHOOK_TOKEN`, and **both have to be set**: `Settings.has_n8n` requires a token, so a blank one keeps the provider local no matter what else is configured. |
| `MUNSHIJI_API_URL` | memory sync | Where MunshiJi's API is reachable **from n8n**, e.g. `https://munshi-xxxx.onrender.com`. Not `localhost` — Cloud cannot see your machine. |
| `MUNSHIJI_MERCHANT_ID` | memory sync | Optional; unset resolves to `default`, the only seeded merchant. |
| `COGNEE_BASE_URL` | memory sync | Your Cognee tenant URL, no trailing slash. |
| `WHATSAPP_API_URL`, `WHATSAPP_TOKEN` | actions | Meta Cloud API send endpoint + bearer token. Only needed once you enable the send node. |

3. **Credentials → New → Header Auth**, named exactly `Cognee API`:
   - **Name** `Authorization`
   - **Value** `Bearer <your cognee key>`

   If the tenant answers **401** with that, edit the same credential to **Name** `X-Api-Key`,
   **Value** `<bare key, no Bearer>` — managed Cognee tenants differ on which header they read,
   and it is one UI edit rather than three node edits.

   Then open each of the three `Cognee:` nodes and pick it. The import deliberately leaves the
   slot empty — a credential id exported from another instance would not resolve here, and a key
   pasted into a workflow file is a key that ends up in git.

4. **Activate** both workflows.

5. Point MunshiJi at n8n — in the `.env` at the **repo root** (not `backend/`):

```
N8N_BASE_URL=https://<your-workspace>.app.n8n.cloud
N8N_WEBHOOK_TOKEN=<the same value as MUNSHIJI_TOKEN>
N8N_WEBHOOK_PREFIX=/webhook
```

`GET /api/health` should now report `n8n: live`.

> **The WhatsApp send node ships disabled.** A freshly imported workflow cannot message real
> people by accident. Enable it deliberately, once the credentials are yours and the recipients
> are meant to hear from you.

### Self-hosted instead

`docker compose up -d n8n`, open <http://localhost:5678>, import the same two files, and set
`N8N_BASE_URL=http://localhost:5678`. One difference: Variables are a licensed feature when
self-hosting, so either set them in the UI if your licence includes them, or swap `$vars.` back
to `$env.` and pass the values as container environment. Cloud is the other way round — `$env` is
blocked in nodes there, which is why these files use `$vars`.

---

## Finale workflows

Three new canvases plus one changed one, built for the same platform rules as the originals:
base nodes only, `$vars` never `$env`, and strictly linear chains (two branches into one node
run that node once per branch; only multiple *triggers* into the first node are safe, because
one fires per execution).

### What changed / what's new

- **`munshiji-memory-sync.json` (changed).** `Cognee: Add Data` now POSTs one document per call
  to `/api/v1/add_text` — the managed platform rejects the batched `{data: [...]}` shape on
  `/add` — so `executeOnce` is gone and the node runs once per incoming item. The read-back
  assertion also accepts the managed platform's singular `search_result`/`searchResult` keys,
  the verify query is the demo centerpiece ("Which customers returned after the last win-back
  offer, and what did they buy?" — a green run of that node *is* a demo beat), and the search
  body sends `topK` alongside `top_k`.
- **`munshiji-digest.json` — MunshiJi — Shaam ka Hisaab.** Cron `0 19 * * *` Asia/Kolkata (and a
  token-checked `POST /webhook/munshiji-digest` for running it on cue). Fetches merchant →
  dashboard → insights → pending actions in one chain, composes a single message in the
  merchant's language, and sends it as a WhatsApp **Send and Wait for a Response** (Approval)
  to the merchant's own phone — approve button **हाँ भेज दो**, disapprove **अभी नहीं**, 12-hour
  wait limit. A tap on हाँ भेज दो approves the pending action through the same
  `POST /api/actions/{id}/approve` gate the app uses. Note it reads the *stored* insight feed,
  never `POST /insights/default/refresh` — an on-stage canvas contains zero unbounded-latency
  nodes, and a test pins that.
- **`munshiji-heartbeat.json`.** Schedule every 5 minutes → `GET /api/health`. Render's free
  tier sleeps after ~15 idle minutes and wakes with a ~50-second cold start; this keeps the
  dyno warm through the other teams' slots. Retire it on a paid instance.
- **`munshiji-evals.json` — MunshiJi — Report Card** + `munshiji-evals-dataset.csv`. An
  Evaluation Trigger reads a **Data Table** named `munshiji-evals` (create it under *Data
  tables*, import the CSV, then pick it in the node — the table id is instance-specific).
  Each of the 15 rows (Hindi and English merchant questions) goes through `POST /api/chat`;
  a Code node scores `tool_match`, `fact_present`, `language_ok` and `latency_ms`; an
  Evaluation node records all four as custom metrics.

### Variables

| Variable | Needed by |
|---|---|
| `MUNSHIJI_TOKEN` | actions, digest (webhook trigger only — the schedule run has no headers and skips the check) |
| `MUNSHIJI_API_URL` | memory sync, digest, heartbeat, evals |
| `MUNSHIJI_MERCHANT_ID` | memory sync (optional, defaults to `default`) |
| `COGNEE_BASE_URL` | memory sync |
| `MERCHANT_WHATSAPP` | digest — the merchant's own phone, E.164 (`+91…`). Until it is set the digest cannot message anyone. |
| `WHATSAPP_API_URL`, `WHATSAPP_TOKEN` | actions (the customer-facing sender, which still ships disabled) |

The Cognee credential is unchanged: Header Auth named exactly `Cognee API`, `Authorization` /
`Bearer <key>` first, and if the tenant answers 401, one UI edit to `X-Api-Key` / `<bare key>`.

### Import order

1. `munshiji-heartbeat.json` — activate first; everything else assumes a warm API.
2. `munshiji-actions.json` and `munshiji-memory-sync.json` — the contract pair, as above.
3. `munshiji-digest.json` — needs `MERCHANT_WHATSAPP` and a Meta credential picked in the
   WhatsApp node (the import leaves the credential slot and the sender's Phone Number ID empty
   on purpose).
4. `munshiji-evals.json` — last, after the `munshiji-evals` Data Table exists and is selected
   in the trigger.

### Meta WhatsApp test-number checklist (do this the night before)

1. In the Meta developer app, use the **free test number** WhatsApp provides — no business
   verification needed.
2. Add and **verify up to 5 recipient numbers** (the test number's allow-list). Both demo
   phones go on it.
3. Both demo phones must **message the test number first** — that opens the 24-hour customer
   service window; outside it, free-form messages (which is what send-and-wait sends) are
   refused.
4. Open the digest's WhatsApp node, pick your Meta credential, select the test number as
   **Phone Number ID**, and set `MERCHANT_WHATSAPP` to a verified demo phone.
5. If Meta stalls anyway, swap that single node for a **Telegram sendAndWait** — same
   mechanics, same approve/disapprove buttons, and the rest of the canvas does not change.

---

## The contract

MunshiJi POSTs to `{N8N_BASE_URL}{N8N_WEBHOOK_PREFIX}/{path}` where `path` is one of
`munshiji-winback`, `munshiji-reminder`, `munshiji-payment-link`, `munshiji-restock`,
`munshiji-followup` — one webhook node each, all feeding the same pipeline.

Headers: `X-Munshiji-Token`, `Idempotency-Key`.

```jsonc
{
  "version": 1,
  "action_id": "act_…", "merchant_id": "mer_…",
  "merchant": { "id": "…", "shop_name": "…", "owner_name": "…", "phone": "…", "language": "hi", "city": "Delhi" },
  "tool": "send_udhaar_reminder",
  "webhook": "munshiji-reminder",
  "idempotency_key": "mer_…:act_…:send_udhaar_reminder",
  "language": "hi",
  "params": { "tone": "standard", "…": "…" },
  "summary": { "en": "Remind 4 customers", "hi": "4 ग्राहकों को याद दिलाएँ" },
  "target_count": 4,
  "targets": [
    {
      "customer_id": "cus_…", "name": "Ramesh Gupta", "phone": "+919810000001",
      "message": "नमस्ते Ramesh Gupta जी, …",     // already rendered in `language`
      "message_hi": "…", "message_en": "…",
      "deliverable": true,
      "amount_paise": 250000, "amount_display": "₹2,500",
      "khata_entry_id": "kht_…", "days_overdue": 52
    }
  ],
  "meta": { "source": "munshiji", "requested_at": "2026-09-14T09:12:00Z", "approved": true }
}
```

The payload is **complete**: the workflow never has to look anything up. Phone numbers arrive
E.164-normalised, message text arrives already rendered per recipient, and undeliverable rows are
filtered out before the call.

### What to respond

```json
{ "ok": true, "delivered": 3, "failed": 0, "id": "wamid.…",
  "results": [{ "customer_id": "cus_…", "ok": true }] }
```

The parser is tolerant — it accepts `success`/`status`, `sent`/`messages_sent`,
`errors`/`messages_failed`, a one-element array, a `{"data": {…}}` wrapper, an empty 200, or n8n's
own `{"message": "Workflow was started"}` — but the shape above is the one to aim for.

Include `results` if you can: with it, only the khata entries that genuinely went out get their
reminder counter bumped. Without it, MunshiJi credits the first `delivered` targets in order.

### Failures

One retry on connect/timeout/5xx, then `ProviderUnavailableError` and a fall back to the local
provider. A 4xx is **not** retried — that is a token or inactive-workflow problem, and retrying
it just delays the diagnosis. The HTTP call sits between two short database transactions, so a
dead n8n leaves zero rows half-written.

---

## Four deliberate choices worth defending

**The approval check is duplicated.** MunshiJi will not dispatch an action that is not in
`APPROVED`, and the workflow refuses any payload without `meta.approved === true`. Two locks on
the same door, on opposite sides of the network — because the expensive failure here is messaging
a customer the merchant never agreed to contact.

**The Cognee read-back is an assertion, not decoration.** `Cognee: Search (verify)` runs after
every cognify, and the workflow throws if it comes back empty. A memory layer that quietly stops
remembering is worse than one that is visibly down: the next conversation would lose its history
without anyone noticing.

**Cognee is called over plain HTTP, not through `n8n-nodes-cognee`.** The community node is
unverified, and n8n Cloud installs verified nodes only — so the node version of this workflow
cannot run on the platform most teams actually have. The HTTP nodes hit the same REST surface
that `munshiji/integrations/cognee_client.py` targets, which also means the API shape is
described in exactly one place in this repo. Both spellings of the drifting keys
(`searchType`/`search_type`, `datasetName`/`dataset_name`) go out together; a server ignores what
it does not recognise.

**The sync is a chain, not a fan-out.** `Fetch actions` and `Fetch insights` used to run side by
side into `Build documents`. Two branches arriving at one node make that node execute once per
branch, so every document would have been ingested twice and the graph would have counted each
action as two. Chaining them costs one round trip and removes the class of bug entirely.
