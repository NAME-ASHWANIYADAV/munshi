# n8n workflows

Two importable workflows. MunshiJi runs perfectly without them — the offline `LocalActions`
provider implements the same contract in-process — so bring these up when you want the real
orchestration leg live (SPEC.md §2.1).

| File | What it does |
|---|---|
| `munshiji-actions.json` | Receives every approved action and fans it out to WhatsApp, reporting delivery back |
| `munshiji-memory-sync.json` | Pushes the shop's actions and findings into Cognee: **Add Data → Cognify → Search** |

---

## Bring up n8n

```bash
docker compose up -d n8n
```

Open <http://localhost:5678>, create the owner account, then:

1. **Settings → Community Nodes → Install** → `n8n-nodes-cognee`
   (needed only by the memory-sync workflow; add a Cognee credential with your
   `platform.cognee.ai` API key afterwards).
2. **Workflows → Import from File** → import both JSON files from this folder.
3. Set the workflow variables / environment the nodes read:

| Variable | Used by | Notes |
|---|---|---|
| `MUNSHIJI_TOKEN` | actions | Must equal `N8N_WEBHOOK_TOKEN` in MunshiJi's `.env`. Leave both blank for a local dry run. |
| `WHATSAPP_API_URL`, `WHATSAPP_TOKEN` | actions | Meta Cloud API send endpoint + bearer token. Swap the HTTP node for a Twilio node if you prefer. |
| `MUNSHIJI_API_URL` | memory sync | e.g. `http://host.docker.internal:8000` |
| `MUNSHIJI_MERCHANT_ID` | memory sync | Optional; `default` resolves to the only seeded merchant. |

4. **Activate** both workflows.
5. Point MunshiJi at them — in `.env`:

```
N8N_BASE_URL=http://localhost:5678
N8N_WEBHOOK_TOKEN=<the same value as MUNSHIJI_TOKEN>
```

`GET /api/health` should now report `n8n: live`.

> **The WhatsApp send node ships disabled.** A freshly imported workflow cannot message real
> people by accident. Enable it deliberately, once the credentials are yours and the recipients
> are meant to hear from you.

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

## Two deliberate choices worth defending

**The approval check is duplicated.** MunshiJi will not dispatch an action that is not in
`APPROVED`, and the workflow refuses any payload without `meta.approved === true`. Two locks on
the same door, on opposite sides of the network — because the expensive failure here is messaging
a customer the merchant never agreed to contact.

**The Cognee read-back is an assertion, not decoration.** `Cognee: Search (verify)` runs after
every cognify, and the workflow throws if it comes back empty. A memory layer that quietly stops
remembering is worse than one that is visibly down: the next conversation would lose its history
without anyone noticing.
