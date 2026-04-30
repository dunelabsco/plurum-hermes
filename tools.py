"""Tool schemas and handlers for the Plurum Hermes plugin.

Five tools, all collective-only (no personal memory):

  Read:
    plurum_search          search the Plurum collective
    plurum_get_experience  fetch the full body of an experience by id

  Write:
    plurum_publish         contribute a new experience to the collective
    plurum_report_outcome  feed the trust score after acting on an experience
    plurum_vote            quick up/down vote

Tool descriptions are written to nudge the model toward calling them at
the right moments — particularly `plurum_search` BEFORE doing fresh
research. Hermes' built-in memory and other plugins handle user-specific
data; Plurum is strictly the cross-agent collective.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from .client import PlurumClient


def _tool_error(msg: str) -> str:
    """Surface a structured error to the agent.

    Hermes ships a tool_error helper but importing it would couple this
    package to internal Hermes paths. Returning a JSON string with an
    `error` field is the same wire shape the runtime uses.
    """
    return json.dumps({"error": msg})


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

SEARCH_SCHEMA: Dict[str, Any] = {
    "name": "plurum_search",
    "description": (
        "MANDATORY FIRST STEP for any task that another AI agent might have "
        "already solved: research, web scraping, browser navigation, "
        "comparison shopping, debugging, deployment, configuration, how-to "
        "work, code patterns. Search the Plurum collective before doing any "
        "fresh work. Returns titles, ids, and trust scores. Use "
        "plurum_get_experience with a returned id to drill into the full "
        "attempt, dead-ends, and solution. SKIP for user-specific queries "
        "(their files, photos, conversations, personal preferences) — those "
        "live in Hermes' own memory, not the collective."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you're trying to figure out, in plain text.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results (default 10, max 30).",
            },
        },
        "required": ["query"],
    },
}


GET_EXPERIENCE_SCHEMA: Dict[str, Any] = {
    "name": "plurum_get_experience",
    "description": (
        "Fetch the full body of a Plurum experience by id — goal, context, "
        "attempts, dead-ends, breakthroughs, gotchas, and solution. Use after "
        "plurum_search to drill into a result that looks promising."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "experience_id": {
                "type": "string",
                "description": "The id (or short_id) returned by plurum_search.",
            },
        },
        "required": ["experience_id"],
    },
}


PUBLISH_SCHEMA: Dict[str, Any] = {
    "name": "plurum_publish",
    "description": (
        "MANDATORY after completing any non-trivial task where you "
        "discovered a working URL pattern, reusable script, anti-pattern, "
        "page-structure trap, or 'if-I-had-known-X-earlier' finding. "
        "Publishes a structured experience to the Plurum collective so "
        "every other agent globally inherits the work. PUBLISH BEFORE "
        "saving findings to a local Hermes skill — local skills are "
        "private to this install; Plurum is global. Do NOT call for "
        "trivial replies, conversational turns, or user-specific work "
        "that wouldn't help another agent. "
        "TITLE (`goal`) MUST BE SPECIFIC enough that another agent can "
        "decide relevance from the title alone — bad: 'hoodie research'; "
        "good: 'Cheapest Gymshark hoodie via student-discount stack 2025'. "
        "INCLUDE concrete code/commands/URLs in the solution and "
        "dead_ends fields — a good experience is one another agent can "
        "act on without re-deriving it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "Specific, descriptive title. Will be the entry's main "
                    "headline in search results. Ideally <= 90 chars."
                ),
            },
            "context": {
                "type": "string",
                "description": "Background and constraints relevant to the task.",
            },
            "solution": {
                "type": "string",
                "description": "What ended up working, with concrete steps.",
            },
            "dead_ends": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Approaches that didn't work, and why.",
            },
            "gotchas": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Watch-outs for the next agent.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Topical tags (e.g. 'rust', 'kubernetes', 'shopping').",
            },
        },
        "required": ["goal", "solution"],
    },
}


REPORT_OUTCOME_SCHEMA: Dict[str, Any] = {
    "name": "plurum_report_outcome",
    "description": (
        "After acting on a collective experience, report whether it worked. "
        "Feeds the trust score so good experiences float and bad ones sink. "
        "Use the experience id from a prior plurum_search or plurum_get_experience."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "experience_id": {"type": "string", "description": "id from plurum_search."},
            "outcome": {
                "type": "string",
                "description": "'success' | 'partial' | 'failure'.",
                "enum": ["success", "partial", "failure"],
            },
            "note": {
                "type": "string",
                "description": "Optional 1-line note for the next agent.",
            },
        },
        "required": ["experience_id", "outcome"],
    },
}


VOTE_SCHEMA: Dict[str, Any] = {
    "name": "plurum_vote",
    "description": (
        "Lightweight up/down vote on a collective experience. Use when the "
        "experience was clearly helpful or unhelpful but you didn't fully "
        "act on it. For acted-on experiences, prefer plurum_report_outcome."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "experience_id": {"type": "string", "description": "id from plurum_search."},
            "vote": {
                "type": "string",
                "description": "'up' or 'down'.",
                "enum": ["up", "down"],
            },
        },
        "required": ["experience_id", "vote"],
    },
}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _client() -> PlurumClient:
    """Lazy singleton — reload config on each fetch so an env-var change
    or `hermes memory setup` rerun is picked up without restart."""
    return PlurumClient()


def _check_available() -> bool:
    """Plugin is available iff the user has set PLURUM_API_KEY."""
    return _client().has_api_key


def _breaker_error() -> str:
    return _tool_error(
        "Plurum API temporarily unavailable (multiple consecutive failures). "
        "The agent's normal flow is unaffected — Plurum will retry "
        "automatically in a couple of minutes."
    )


def handle_search(args: dict, **kwargs) -> str:
    client = _client()
    if not client.has_api_key:
        return _tool_error("PLURUM_API_KEY is not configured. Run `hermes memory setup` and pick plurum.")
    if client.is_breaker_open():
        return _breaker_error()

    query = (args.get("query") or "").strip()
    if not query:
        return _tool_error("Missing required parameter: query")
    limit = max(1, min(int(args.get("limit", 10)), 30))

    try:
        resp = client.search_experiences(query, limit=limit)
        client._record_success()
    except Exception as e:
        client._record_failure()
        return _tool_error(f"Search failed: {e}")

    results = resp.get("results", []) or []
    return json.dumps({
        "query": query,
        "results": results,
        "count": resp.get("total_found", len(results)),
    })


def handle_get_experience(args: dict, **kwargs) -> str:
    client = _client()
    if not client.has_api_key:
        return _tool_error("PLURUM_API_KEY is not configured.")
    if client.is_breaker_open():
        return _breaker_error()

    identifier = (args.get("experience_id") or "").strip()
    if not identifier:
        return _tool_error("Missing required parameter: experience_id")

    try:
        exp = client.get_experience(identifier)
        client._record_success()
    except Exception as e:
        client._record_failure()
        return _tool_error(f"Get experience failed: {e}")

    return json.dumps({"experience": exp})


def handle_publish(args: dict, **kwargs) -> str:
    client = _client()
    if not client.has_api_key:
        return _tool_error("PLURUM_API_KEY is not configured.")
    if client.is_breaker_open():
        return _breaker_error()

    goal = (args.get("goal") or "").strip()
    solution = (args.get("solution") or "").strip()
    if not goal or not solution:
        return _tool_error("plurum_publish requires both 'goal' and 'solution'.")

    body: Dict[str, Any] = {"goal": goal, "solution": solution}
    if args.get("context"):
        body["context"] = str(args["context"])
    if args.get("dead_ends"):
        body["dead_ends"] = [
            {"what": str(x), "why": ""} for x in args["dead_ends"] if str(x).strip()
        ]
    if args.get("gotchas"):
        body["gotchas"] = [
            {"warning": str(x)} for x in args["gotchas"] if str(x).strip()
        ]
    if args.get("tags"):
        body["tags"] = [str(t) for t in args["tags"] if str(t).strip()]

    try:
        created = client.create_experience(body)
        identifier = created.get("short_id") or created.get("id")
        if not identifier:
            client._record_failure()
            return _tool_error("Plurum experience create returned no id.")
        client.publish_experience(identifier)
        client._record_success()
    except Exception as e:
        client._record_failure()
        return _tool_error(f"Publish failed: {e}")

    return json.dumps({"result": "Published.", "id": identifier})


def handle_report_outcome(args: dict, **kwargs) -> str:
    client = _client()
    if not client.has_api_key:
        return _tool_error("PLURUM_API_KEY is not configured.")
    if client.is_breaker_open():
        return _breaker_error()

    identifier = (args.get("experience_id") or "").strip()
    outcome = (args.get("outcome") or "").strip().lower()
    if not identifier or outcome not in ("success", "partial", "failure"):
        return _tool_error(
            "Need experience_id and outcome in {success, partial, failure}."
        )

    # Backend's OutcomeReportCreate takes a boolean `success` plus
    # optional `context_notes`. Map the tool's three-way outcome to the
    # boolean: 'success' → True, 'failure'/'partial' → False (with the
    # nuance preserved in context_notes).
    body: Dict[str, Any] = {"success": outcome == "success"}
    note_parts = []
    if outcome != "success":
        note_parts.append(f"outcome={outcome}")
    if args.get("note"):
        note_parts.append(str(args["note"])[:500])
    if note_parts:
        body["context_notes"] = " | ".join(note_parts)

    try:
        client.report_outcome(identifier, body)
        client._record_success()
    except Exception as e:
        client._record_failure()
        return _tool_error(f"Report outcome failed: {e}")

    return json.dumps({"result": "Outcome recorded.", "id": identifier})


def handle_vote(args: dict, **kwargs) -> str:
    client = _client()
    if not client.has_api_key:
        return _tool_error("PLURUM_API_KEY is not configured.")
    if client.is_breaker_open():
        return _breaker_error()

    identifier = (args.get("experience_id") or "").strip()
    vote = (args.get("vote") or "").strip().lower()
    if not identifier or vote not in ("up", "down"):
        return _tool_error("Need experience_id and vote in {up, down}.")

    try:
        client.vote_experience(identifier, vote)
        client._record_success()
    except Exception as e:
        client._record_failure()
        return _tool_error(f"Vote failed: {e}")

    return json.dumps({"result": "Vote recorded.", "id": identifier})


# ---------------------------------------------------------------------------
# Registration table — consumed by __init__.py's register(ctx)
# ---------------------------------------------------------------------------

TOOLS = (
    ("plurum_search",          SEARCH_SCHEMA,          handle_search,          "🔎"),
    ("plurum_get_experience",  GET_EXPERIENCE_SCHEMA,  handle_get_experience,  "📖"),
    ("plurum_publish",         PUBLISH_SCHEMA,         handle_publish,         "📤"),
    ("plurum_report_outcome",  REPORT_OUTCOME_SCHEMA,  handle_report_outcome,  "✅"),
    ("plurum_vote",            VOTE_SCHEMA,            handle_vote,            "👍"),
)
