"""End-to-end API tests.

Runs the real application against a freshly seeded temporary database, entirely offline. These
are the tests that would catch a route wired to a schema it does not actually satisfy - the class
of break that only shows up when the frontend calls it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from munshiji.db import base as db_base
from munshiji.db.enums import ActionStatus


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """The real app, on a seeded throwaway database, with every provider forced local."""
    db_path = tmp_path_factory.mktemp("api") / "api.db"

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

        reset_providers()
        reset_db()
        with session_scope() as session:
            generate(session, days=60)

        from munshiji.main import create_app

        with TestClient(create_app()) as test_client:
            yield test_client

        reset_providers()
        db_base.reset_engine()
        get_settings.cache_clear()


@pytest.fixture(scope="module")
def merchant_id(client: TestClient) -> str:
    return client.get("/api/merchant/default").json()["id"]


# ── Health ──────────────────────────────────────────────────────────────────


def test_health_reports_every_provider_and_the_sponsor_view(client: TestClient) -> None:
    body = client.get("/api/health").json()

    assert body["status"] in {"ok", "degraded"}
    assert body["database_ready"] is True
    assert {entry["kind"] for entry in body["providers"]} == {
        "llm",
        "stt",
        "tts",
        "memory",
        "actions",
    }
    # With no credentials configured, everything must be serving from the offline path.
    assert {entry["mode"] for entry in body["providers"]} == {"local"}
    assert set(body["sponsors"]) == {"sarvam", "cognee", "n8n"}


# ── Merchant and dashboard ──────────────────────────────────────────────────


def test_merchant_profile(client: TestClient, merchant_id: str) -> None:
    body = client.get(f"/api/merchant/{merchant_id}").json()
    assert body["shop_name"]
    assert body["language"].startswith("hi")


def test_dashboard_money_crosses_the_wire_pre_formatted(client: TestClient) -> None:
    body = client.get("/api/merchant/default/dashboard").json()

    collected = body["today"]["collected"]
    assert set(collected) == {"paise", "display", "short"}
    assert isinstance(collected["paise"], int)
    assert collected["display"].startswith("₹")

    assert len(body["sparkline"]) == 14
    assert body["sparkline"][-1]["is_today"] is True
    assert 0.0 <= body["today"]["day_progress"] <= 1.0
    assert sum(slice_["share_pct"] for slice_ in body["payment_mix"]) == pytest.approx(
        100.0, abs=1.5
    )


def test_unknown_merchant_is_a_clean_404(client: TestClient) -> None:
    response = client.get("/api/merchant/mer_nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# ── Insights ────────────────────────────────────────────────────────────────


def test_refresh_then_list_insights(client: TestClient, merchant_id: str) -> None:
    refreshed = client.post(f"/api/insights/{merchant_id}/refresh").json()
    assert refreshed["insights"], "the seeded shop must produce findings"

    first = refreshed["insights"][0]
    assert first["title_hi"] and first["title_en"]
    assert first["score"] > 0
    assert set(first["impact"]) == {"paise", "display", "short"}

    listed = client.get(f"/api/insights/{merchant_id}").json()
    assert [item["id"] for item in listed["insights"]] == [
        item["id"] for item in refreshed["insights"]
    ]
    # Ranked, highest score first.
    scores = [item["score"] for item in listed["insights"]]
    assert scores == sorted(scores, reverse=True)


# ── Conversation ────────────────────────────────────────────────────────────


def test_chat_answers_with_a_tool_backed_number(client: TestClient, merchant_id: str) -> None:
    body = client.post(
        "/api/chat", json={"merchant_id": merchant_id, "text": "Aaj dhandha kaisa raha?"}
    ).json()

    assert body["reply"]
    assert body["conversation_id"]
    assert body["tool_calls"], "a sales question must be answered from a tool, not from memory"
    assert body["tool_calls"][0]["name"] == "get_sales_summary"
    assert body["meta"]["provider"] == "local"


def test_conversation_id_threads_turns_together(client: TestClient, merchant_id: str) -> None:
    first = client.post(
        "/api/chat", json={"merchant_id": merchant_id, "text": "Aaj ka collection?"}
    ).json()
    second = client.post(
        "/api/chat",
        json={
            "merchant_id": merchant_id,
            "text": "Aur udhaar kitna baaki hai?",
            "conversation_id": first["conversation_id"],
        },
    ).json()
    assert second["conversation_id"] == first["conversation_id"]


# ── Actions: the approval gate, over HTTP ───────────────────────────────────


def test_proposing_and_approving_an_action(client: TestClient, merchant_id: str) -> None:
    client.post(f"/api/insights/{merchant_id}/refresh")

    proposed = client.post(
        "/api/chat",
        json={"merchant_id": merchant_id, "text": "Purane customers ko 10% ka offer bhej do"},
    ).json()

    action = proposed["pending_action"]
    assert action is not None, "a write tool must surface a proposal, not send anything"
    assert action["status"] == ActionStatus.PENDING_APPROVAL.value
    assert action["requires_approval"] is True
    assert action["target_count"] > 0

    executed = client.post(
        f"/api/actions/{action['id']}/approve", json={"approved_by": "merchant"}
    ).json()
    assert executed["status"] == ActionStatus.EXECUTED.value
    assert executed["executed_at"]
    assert executed["result"]["delivered_count"] >= 1
    assert any(outcome["metric"] == "messages_sent" for outcome in executed["outcomes"])


def test_approving_twice_is_refused(client: TestClient, merchant_id: str) -> None:
    proposed = client.post(
        "/api/chat",
        json={"merchant_id": merchant_id, "text": "Dormant customers ko offer bhejo"},
    ).json()
    action = proposed["pending_action"]
    if action is None:  # every dormant customer may already be in this run's audience
        pytest.skip("no fresh proposal available in this run")

    assert client.post(f"/api/actions/{action['id']}/approve", json={}).status_code == 200
    second = client.post(f"/api/actions/{action['id']}/approve", json={})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "invalid_state_transition"


def test_rejecting_an_action(client: TestClient, merchant_id: str) -> None:
    proposed = client.post(
        "/api/chat",
        json={"merchant_id": merchant_id, "text": "Udhaar wale customers ko reminder bhejo"},
    ).json()
    action = proposed["pending_action"]
    if action is None:
        pytest.skip("no reminder proposal available in this run")

    rejected = client.post(
        f"/api/actions/{action['id']}/reject", json={"reason": "abhi rehne do"}
    ).json()
    assert rejected["status"] == ActionStatus.REJECTED.value
    assert rejected["result"]["reason"] == "abhi rehne do"


def test_action_feed_lists_what_happened(client: TestClient, merchant_id: str) -> None:
    body = client.get(f"/api/actions/{merchant_id}").json()
    assert body["actions"]
    assert {action["status"] for action in body["actions"]} & {
        ActionStatus.EXECUTED.value,
        ActionStatus.REJECTED.value,
    }


# ── Memory ──────────────────────────────────────────────────────────────────


def test_memory_search_returns_hits_with_provenance(client: TestClient, merchant_id: str) -> None:
    body = client.post(
        f"/api/memory/{merchant_id}/search",
        json={"query": "offer", "limit": 5, "hops": 1},
    ).json()

    assert body["query"] == "offer"
    assert body["hits"], "the executed offer must be recallable"
    assert body["rendered"]
    assert all("ref" in hit and "kind" in hit for hit in body["hits"])


def test_memory_graph_is_keyed_by_ref(client: TestClient, merchant_id: str) -> None:
    body = client.get(f"/api/memory/{merchant_id}/graph?limit=50").json()
    assert body["nodes"]
    refs = {node["ref"] for node in body["nodes"]}
    for edge in body["edges"]:
        assert edge["source"] in refs or edge["target"] in refs


# ── Voice ───────────────────────────────────────────────────────────────────


def test_speak_returns_playable_audio_or_delegates_to_the_client(client: TestClient) -> None:
    body = client.post("/api/voice/speak", json={"text": "आज का गल्ला", "language": "hi-IN"}).json()
    assert body["client_should_synthesise"] or body["audio_data_uri"]
    assert body["duration_ms"] > 0


def test_transcribe_round_trips_the_offline_envelope(client: TestClient) -> None:
    from munshiji.providers.stt_local import encode_text_wav

    audio = encode_text_wav("aaj dhandha kaisa raha")
    response = client.post(
        "/api/voice/transcribe",
        files={"file": ("utterance.wav", audio, "audio/wav")},
        data={"language": "hi-IN"},
    )
    assert response.status_code == 200
    assert "dhandha" in response.json()["text"]


# ── Events ──────────────────────────────────────────────────────────────────


def test_event_ping_reports_subscriber_count(client: TestClient, merchant_id: str) -> None:
    body = client.post(f"/api/events/{merchant_id}/ping").json()
    assert body["ok"] is True
    assert body["subscribers"] >= 0


# ── Contract ────────────────────────────────────────────────────────────────


def test_openapi_documents_every_route(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    for expected in (
        "/api/health",
        "/api/auth/login",
        "/api/auth/shops",
        "/api/merchant/{merchant_id}",
        "/api/merchant/{merchant_id}/dashboard",
        "/api/insights/{merchant_id}",
        "/api/insights/{merchant_id}/refresh",
        "/api/chat",
        "/api/voice/transcribe",
        "/api/voice/speak",
        "/api/actions/{merchant_id}",
        "/api/actions/{action_id}/approve",
        "/api/actions/{action_id}/reject",
        "/api/memory/{merchant_id}/search",
        "/api/memory/{merchant_id}/graph",
        "/api/events/{merchant_id}",
    ):
        assert expected in paths, f"{expected} missing from the OpenAPI contract"
