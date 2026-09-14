"""Ad-hoc foundation smoke check (not part of the pytest suite).

Run: ``backend/.venv/Scripts/python.exe tests/smoke_foundation.py``
"""

from __future__ import annotations

from munshiji.clock import business_day_progress, day_bounds_ist, now_ist, today_ist
from munshiji.config import get_settings
from munshiji.db import models
from munshiji.db.base import reset_db
from munshiji.ids import new_id, timestamp_of
from munshiji.money import fmt_inr, fmt_inr_short, group_indian, paise_from_rupees
from munshiji.providers import actions, base, llm, memory, stt, tts  # noqa: F401


def main() -> None:
    settings = get_settings()
    print("settings:", settings.env, settings.provider_mode)
    print("db url  :", settings.sqlalchemy_url)

    print("money   :", fmt_inr(1854000), "|", fmt_inr(123456789), "|", fmt_inr_short(123456789))
    print("grouping:", group_indian("1234567"), "|", group_indian("999"))
    print("parse   :", paise_from_rupees("18540.50"))

    print("ist now :", now_ist().isoformat(timespec="seconds"))
    print("bounds  :", [b.isoformat() for b in day_bounds_ist(today_ist())])
    print("progress:", round(business_day_progress(), 3))

    identifier = new_id("cus")
    print("id      :", identifier, "->", timestamp_of(identifier))

    reset_db()
    print("tables  :", ", ".join(sorted(models.Base.metadata.tables)))
    print("OK")


if __name__ == "__main__":
    main()
