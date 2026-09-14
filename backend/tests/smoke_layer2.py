"""Ad-hoc import check for the hand-written layer.

Covers schemas, events, the approval gate, prompts and the API dependencies.

Run: ``backend/.venv/Scripts/python.exe tests/smoke_layer2.py``
"""

from __future__ import annotations

from munshiji.agent import approval, prompts
from munshiji.api import deps  # noqa: F401
from munshiji.api.routes import health  # noqa: F401
from munshiji.events import EventName, get_event_bus
from munshiji.providers import factory
from munshiji.schemas import action, common, conversation, insight, memory, merchant
from munshiji.schemas.health import HealthOut


def main() -> None:
    print(
        "schemas :",
        [
            m.__name__.split(".")[-1]
            for m in (common, merchant, insight, action, conversation, memory)
        ],
    )
    print("money   :", common.money(1854000).model_dump())

    bus = get_event_bus()
    bus.publish("mer_x", EventName.HEARTBEAT, {"hello": "world"})
    print("bus     : ok, subscribers =", bus.subscriber_count("mer_x"))

    print("write tools:", sorted(approval.WRITE_TOOLS))
    print("transitions:", len(approval.ALLOWED_TRANSITIONS))

    from munshiji.db.models import Merchant

    shop = Merchant(
        owner_name="Rajesh Sharma",
        shop_name="Sharma General Store",
        category="kirana",
        city="Delhi",
        locality="Lajpat Nagar",
        language="hi-IN",
    )
    system = prompts.build_system_prompt(
        shop,
        snapshot={"collection so far": "₹11,240", "vs baseline": "-12.4%"},
        memory="- 12 Sep: offer sent to 12 customers; 4 returned; ₹2,340 recovered",
        language="hi-IN",
    )
    print("prompt chars:", len(system))
    print("prompt head :", system.splitlines()[0])

    print("health model:", HealthOut().status)
    print("factory api :", [n for n in dir(factory) if not n.startswith("_")][:6])
    print("OK")


if __name__ == "__main__":
    main()
