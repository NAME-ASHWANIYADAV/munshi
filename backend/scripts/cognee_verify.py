"""Does the managed Cognee tenant actually speak the API our client was written against?

`munshiji health` only runs a reachability probe, which can pass on a deployment whose
add/cognify/search payloads differ from the open-source server ours was modelled on. This walks
the real round trip on a throwaway dataset and says exactly which call breaks and how, so a
shape mismatch is a five-minute fix in cognee_client.py tonight rather than a mystery on stage.

Run it after putting COGNEE_API_KEY in the .env at the REPO ROOT (not backend/):

    backend/.venv/Scripts/python.exe backend/scripts/cognee_verify.py

Then run it AGAIN with --hindi once the real graph is built, to answer the question that gates
the demo centerpiece: does GRAPH_COMPLETION join atomic facts when asked in Hindi?
"""

from __future__ import annotations

import asyncio
import sys

from munshiji.config import get_settings

PROBE_DATASET = "munshiji_probe"

# Three ATOMIC documents. The whole point: the answer to the probe question exists in no single
# one of them — if search joins offer -> Sunita -> return visit, multi-hop is real on this
# tenant and the demo claim is honest. If it comes back empty, the pitch line changes tonight.
PROBE_DOCS = [
    ("probe:action:1", "Win-back offer", "Shop Sharma General Store sent a win-back offer to customer Sunita Devi on 10 September 2026."),
    ("probe:visit:1", "Return visit", "Customer Sunita Devi visited Sharma General Store on 14 September 2026 and spent 540 rupees on groceries."),
    ("probe:khata:1", "Khata state", "Customer Sunita Devi has no outstanding udhaar at Sharma General Store."),
]

PROBE_QUESTION_EN = "Which customers returned after the win-back offer, and how much did they spend?"
PROBE_QUESTION_HI = "पिछली बार जो ऑफर भेजा था, उनमें से कौन वापस आया और उन्होंने कितना खर्च किया?"


async def main() -> int:
    settings = get_settings()
    key = settings.cognee_api_key.strip()
    hindi = "--hindi" in sys.argv

    print("=" * 68)
    print(f"base url   {settings.cognee_base_url}")
    print(f"api key    {'set (' + str(len(key)) + ' chars)' if key else 'MISSING - nothing to test'}")
    print("=" * 68)
    if not key:
        print("\nPut COGNEE_API_KEY in the .env at the repo root, then run this again.")
        return 1

    from munshiji.integrations.cognee_client import CogneeClient, CogneeDocument

    failures = 0

    async with CogneeClient() as client:

        async def step(name: str, coro):
            nonlocal failures
            print(f"\n[{name}]")
            try:
                result = await coro
                print(f"  ok  {repr(result)[:500]}")
                return result
            except Exception as exc:
                failures += 1
                print(f"  FAILED  {type(exc).__name__}: {exc}")
                return None

        await step("ping", client.ping())

        docs = [CogneeDocument(ref=r, title=t, text=x) for r, t, x in PROBE_DOCS]
        await step("add (3 atomic docs)", client.add(docs, dataset_name=PROBE_DATASET))
        await step("cognify", client.cognify(dataset_name=PROBE_DATASET))

        print("\n  ...waiting 25s for the graph to settle (cognify can be async)...")
        await asyncio.sleep(25)

        question = PROBE_QUESTION_HI if hindi else PROBE_QUESTION_EN
        hits = await step(f"search GRAPH_COMPLETION ({'hi' if hindi else 'en'})",
                          client.search(question, dataset_name=PROBE_DATASET))

        await step("graph export", client.graph(dataset_name=PROBE_DATASET))

    print("\n" + "=" * 68)
    if failures:
        print(f"{failures} step(s) FAILED - paste this whole output back into the chat.")
        return 1
    joined = " ".join(h.text for h in (hits or []))
    if "Sunita" in joined and ("540" in joined or "spend" in joined.lower() or "खर्च" in joined):
        print("MULTI-HOP CONFIRMED: the answer joined the offer doc to the visit doc.")
        print("The 'exists in no single record' demo line is honest on this tenant.")
    else:
        print("Search answered but did NOT visibly join the two facts.")
        print("Decision point: keep composite docs and pitch entity-360 instead of multi-hop.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
