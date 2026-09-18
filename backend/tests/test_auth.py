"""Login and the multi-shop world.

Two promises under test: the credential check is real (salted hash, 401 on any mismatch, one
indistinguishable error), and seeding several shops never moves the ``"default"`` alias off
Sharma — the n8n workflows and the runbook recipe depend on that anchor.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from munshiji.db import base as db_base
from munshiji.security import hash_password, normalise_phone, verify_password


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """The real app over a database holding all three seeded shops."""
    db_path = tmp_path_factory.mktemp("auth") / "auth.db"

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MUNSHIJI_DB_URL", f"sqlite:///{db_path.as_posix()}")
        patch.setenv("MUNSHIJI_PROVIDER_MODE", "local")
        patch.setenv("SARVAM_API_KEY", "")
        patch.setenv("COGNEE_API_KEY", "")
        patch.setenv("N8N_WEBHOOK_TOKEN", "")

        from munshiji.config import get_settings

        get_settings.cache_clear()
        db_base.reset_engine()

        from munshiji.db.base import reset_db, session_scope
        from munshiji.providers.factory import reset_providers
        from munshiji.seed.generator import generate
        from munshiji.seed.profiles import SEEDED_PROFILES

        reset_providers()
        reset_db()
        with session_scope() as session:
            for offset, profile in enumerate(SEEDED_PROFILES):
                generate(session, profile, seed=99_2026 + offset, days=60)

        from munshiji.main import create_app

        with TestClient(create_app()) as test_client:
            yield test_client

        reset_providers()
        db_base.reset_engine()
        get_settings.cache_clear()


# ── The hashing primitives ──────────────────────────────────────────────────


def test_normalise_phone_accepts_every_way_a_number_gets_typed() -> None:
    for raw in ("+91 98110 34572", "98110-34572", "09811034572", "9811034572"):
        assert normalise_phone(raw) == "9811034572"


def test_same_password_hashes_differently_per_phone() -> None:
    assert hash_password("9811034572", "munshi123") != hash_password("9873046521", "munshi123")


def test_verify_rejects_an_empty_stored_hash() -> None:
    assert not verify_password("9811034572", "", "")


# ── The endpoints ───────────────────────────────────────────────────────────


def test_shops_lists_all_three_worlds_oldest_first(client: TestClient) -> None:
    body = client.get("/api/auth/shops").json()
    names = [shop["shop_name"] for shop in body["shops"]]
    assert names == ["Sharma General Store", "Gupta Medical Store", "Khan Mobile Point"]
    categories = {shop["category"] for shop in body["shops"]}
    assert categories == {"kirana", "pharmacy", "mobile"}
    assert body["demo_password"]
    assert all("password" not in shop and "password_hash" not in shop for shop in body["shops"])


def test_default_alias_stays_pinned_to_sharma(client: TestClient) -> None:
    merchant = client.get("/api/merchant/default").json()
    assert merchant["shop_name"] == "Sharma General Store"


def test_login_succeeds_for_every_shop_in_any_phone_format(client: TestClient) -> None:
    shops = client.get("/api/auth/shops").json()["shops"]
    for shop in shops:
        spaced = f"+91 {shop['phone'][-10:-5]} {shop['phone'][-5:]}"
        response = client.post(
            "/api/auth/login", json={"phone": spaced, "password": "munshi123"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["merchant"]["id"] == shop["merchant_id"]
        assert body["token"]
        assert "password_hash" not in body["merchant"]


def test_wrong_password_and_unknown_phone_fail_indistinguishably(client: TestClient) -> None:
    wrong = client.post(
        "/api/auth/login", json={"phone": "9811034572", "password": "galat"}
    )
    unknown = client.post(
        "/api/auth/login", json={"phone": "9999999999", "password": "munshi123"}
    )
    assert wrong.status_code == 401 and unknown.status_code == 401
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


def test_each_shop_answers_from_its_own_books(client: TestClient) -> None:
    """The same dashboard endpoint, three different worlds — the multi-tenant proof."""
    shops = client.get("/api/auth/shops").json()["shops"]
    seen = set()
    for shop in shops:
        dashboard = client.get(f"/api/merchant/{shop['merchant_id']}/dashboard").json()
        assert dashboard["merchant"]["shop_name"] == shop["shop_name"]
        seen.add(dashboard["merchant"]["category"])
    assert seen == {"kirana", "pharmacy", "mobile"}
