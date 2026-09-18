"""The n8n workflow files, checked against the platform they actually have to run on.

These JSONs are edited by hand and imported into somebody else's n8n, so nothing here is
caught by the type checker or by importing a module. Every assertion below stands for a way
one of them has already broken, or would break silently on n8n Cloud:

* a raw newline inside a ``jsCode`` string, which makes the file un-importable;
* a community node, which Cloud refuses to install unless it is verified;
* ``$env``, which Cloud blocks in nodes and which *throws* rather than returning undefined;
* two branches meeting at one node, which makes that node execute once per branch;
* a webhook path drifting away from the one MunshiJi POSTs to.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from munshiji.integrations.n8n_client import WEBHOOK_PATHS

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / "workflows"
ACTIONS = WORKFLOW_DIR / "munshiji-actions.json"
MEMORY_SYNC = WORKFLOW_DIR / "munshiji-memory-sync.json"
DIGEST = WORKFLOW_DIR / "munshiji-digest.json"
HEARTBEAT = WORKFLOW_DIR / "munshiji-heartbeat.json"
EVALS = WORKFLOW_DIR / "munshiji-evals.json"
ALL_WORKFLOWS = (ACTIONS, MEMORY_SYNC, DIGEST, HEARTBEAT, EVALS)


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", ALL_WORKFLOWS, ids=lambda p: p.name)
def test_workflow_is_importable_json(path: Path) -> None:
    """n8n rejects the whole file on a single bad escape, with no useful error."""
    document = load(path)
    assert document["nodes"], f"{path.name} has no nodes"
    assert document["connections"], f"{path.name} has no connections"


@pytest.mark.parametrize("path", ALL_WORKFLOWS, ids=lambda p: p.name)
def test_workflow_uses_only_built_in_nodes(path: Path) -> None:
    """n8n Cloud installs verified community nodes only, and ours would not qualify."""
    outside = sorted(
        {
            node["type"]
            for node in load(path)["nodes"]
            if not node["type"].startswith("n8n-nodes-base.")
        }
    )
    assert not outside, (
        f"{path.name} needs community nodes that n8n Cloud will not install: {outside}"
    )


@pytest.mark.parametrize("path", ALL_WORKFLOWS, ids=lambda p: p.name)
def test_workflow_reads_variables_not_environment(path: Path) -> None:
    """Cloud sets N8N_BLOCK_ENV_ACCESS_IN_NODE, so `$env.X` raises instead of being empty."""
    assert "$env." not in path.read_text(encoding="utf-8"), (
        f"{path.name} reads $env, which throws on n8n Cloud - use $vars instead"
    )


@pytest.mark.parametrize("path", ALL_WORKFLOWS, ids=lambda p: p.name)
def test_no_node_is_fed_by_two_branches(path: Path) -> None:
    """Two connections into one input run that node twice, once per arriving branch.

    In the memory sync that meant every document ingested twice and every action counted
    twice in the graph - a wrong answer rather than an error.
    """
    document = load(path)
    triggers = {
        node["name"]
        for node in document["nodes"]
        if node["type"].endswith((".webhook", ".scheduleTrigger"))
    }

    incoming: dict[tuple[str, int], list[str]] = {}
    for source, outputs in document["connections"].items():
        for branch in outputs.get("main", []):
            for target in branch or []:
                incoming.setdefault((target["node"], target.get("index", 0)), []).append(source)

    collisions = {
        target: sources
        for target, sources in incoming.items()
        # Several triggers feeding one entry point is the normal way to offer a workflow both a
        # schedule and an on-demand call: only one of them ever fires per execution.
        if len(sources) > 1 and not set(sources) <= triggers
    }
    assert not collisions, f"{path.name} has nodes fed by two branches: {collisions}"


@pytest.mark.parametrize("path", ALL_WORKFLOWS, ids=lambda p: p.name)
def test_every_connection_names_a_real_node(path: Path) -> None:
    document = load(path)
    names = {node["name"] for node in document["nodes"]}
    referenced = set(document["connections"]) | {
        target["node"]
        for outputs in document["connections"].values()
        for branch in outputs.get("main", [])
        for target in branch or []
    }
    assert referenced <= names, f"{path.name} points at missing nodes: {sorted(referenced - names)}"


def test_actions_workflow_listens_on_exactly_the_paths_munshiji_posts_to() -> None:
    """The one place the Python client and the hand-written JSON have to agree."""
    listening = {
        node["parameters"]["path"]
        for node in load(ACTIONS)["nodes"]
        if node["type"] == "n8n-nodes-base.webhook"
    }
    assert listening == set(WEBHOOK_PATHS)


def test_the_whatsapp_send_node_ships_disabled() -> None:
    """A freshly imported workflow must not be able to message real people by accident.

    Deliberately scoped to munshiji-actions.json: that workflow fans messages out to
    *customers*, people who never opted into anyone's test run. The digest's send-and-wait
    node is exempt and ships enabled, because it messages exactly one number - the merchant's
    own verified phone ($vars.MERCHANT_WHATSAPP) - and stays inert until that variable and a
    credential are set. Do not widen this test to ALL_WORKFLOWS.
    """
    senders = [
        node
        for node in load(ACTIONS)["nodes"]
        if node["type"] == "n8n-nodes-base.httpRequest" and "whatsapp" in node["name"].lower()
    ]
    assert senders, "the WhatsApp send node is gone - did the actions workflow get rewritten?"
    assert all(node.get("disabled") for node in senders)


def test_the_digest_never_calls_the_unbounded_refresh_endpoint() -> None:
    """POST /api/insights/{id}/refresh reruns every insight engine and takes as long as it takes.

    The digest runs on stage at 19:00 with the merchant watching their phone, so every node in
    it must have bounded latency: it reads the stored feed (GET /api/insights/default) and
    leaves recomputation to the backend's own schedule.
    """
    urls = [
        node["parameters"].get("url", "")
        for node in load(DIGEST)["nodes"]
        if node["type"] == "n8n-nodes-base.httpRequest"
    ]
    assert urls, "the digest workflow lost its HTTP nodes - did it get rewritten?"
    offenders = [url for url in urls if "/refresh" in url]
    assert not offenders, f"digest hits unbounded-latency endpoints: {offenders}"


def test_cognee_steps_keep_both_spellings_of_the_drifting_keys() -> None:
    """Cognee releases have used camelCase and snake_case for the same fields.

    ``cognee_client.py`` sends both; the workflow has to as well, or the two integrations
    disagree about the same API and only one of them works after an upgrade.
    """
    bodies = " ".join(
        node["parameters"].get("jsonBody", "")
        for node in load(MEMORY_SYNC)["nodes"]
        if node["name"].startswith("Cognee: ")
    )
    for camel, snake in (("datasetName", "dataset_name"), ("searchType", "search_type")):
        assert camel in bodies and snake in bodies, f"{camel}/{snake} not both sent"
