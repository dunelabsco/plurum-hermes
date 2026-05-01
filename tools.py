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
from .metrics import log_metric


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
        "Search the Plurum collective — structured experiences contributed "
        "by every other agent globally. Call this BEFORE fresh research, "
        "web browsing, scraping, comparison shopping, debugging, deployment, "
        "or how-to work — another agent has often already solved the same "
        "problem. Returns titles, ids, and trust scores; use "
        "plurum_get_experience with a returned id to drill into the full "
        "attempt, dead-ends, and solution. SKIP for user-specific queries "
        "(their files, photos, conversations, personal preferences) — "
        "those live in Hermes' own memory, not the collective."
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
        "Contribute a structured experience to the Plurum collective after "
        "completing any non-trivial task where you discovered a working "
        "URL pattern, reusable script, anti-pattern, page-structure trap, "
        "or 'if-I-had-known-X-earlier' finding. Save your local Hermes "
        "skill as you normally would AND call plurum_publish — local "
        "skills help YOU, Plurum helps EVERYONE. They are additive, not "
        "competitors. Do NOT call for trivial replies, conversational "
        "turns, or user-specific work that wouldn't help another agent. "
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


# Just-in-time reminders embedded in tool responses. The session-start
# directive tells the agent that Plurum exists; these reminders surface
# the workflow at the moment the agent is actually using a Plurum tool —
# the point where the next action (report_outcome, re-search on pivot)
# is most likely to be relevant. Cheaper than per-turn injection because
# they only appear inside tool results, not in every LLM call.
#
# v0.7: reminders are placed at the TOP of the JSON response so they are
# in the agent's first read pass. Live agent feedback: "the reminder is
# at the end of a giant JSON response. By the time I've parsed 10 results
# my attention budget for that tool call is spent."
_SEARCH_REMINDER = (
    "After acting on one of these, call plurum_report_outcome with the "
    "id (success/partial/failure). If the user later pivots to a "
    "different site, store, or platform in this conversation, call "
    "plurum_search again — collective knowledge is per-domain, not "
    "per-conversation."
)
_GET_EXPERIENCE_REMINDER = (
    "When you've finished applying this experience, call "
    "plurum_report_outcome with the id and an outcome of "
    "success/partial/failure (plus a one-line note on what you actually "
    "did). The trust score depends on outcome reports."
)

# Below this rerank-score floor, surface results as an explicit "no prior
# art" signal rather than dump low-relevance noise on the agent. Live
# agent feedback: two consecutive low-quality search hits trained the
# agent to stop calling plurum_search; structurally distinguishing
# "no results" from "bad results" breaks that pattern.
#
# The search RPC reorders by `rerank_score` from the cross-encoder (1-10
# scale), not raw cosine `similarity`. Rerank score is what actually
# determined the ranking, so it's the right field to gate on. v0.7.0 read
# `similarity` (0-1 cosine) — wrong field, wrong scale.
_RERANK_FLOOR = 5.0

# Heavy fields stripped from search results so the agent's context
# isn't burned on full bodies of 10 (mostly irrelevant) experiences per
# search. Full body remains accessible via plurum_get_experience. Live
# agent feedback: "search results should return titles + short_id +
# trust score + 1-line summary; plurum_get_experience should be the
# only way to get the full body."
_SEARCH_RESULT_KEEP_FIELDS = (
    "id", "short_id", "goal", "domain", "tags",
    "trust_score", "rerank_score", "similarity",
    "success_count", "success_rate", "quality_score",
    "created_at",
)


def _trim_search_result(r: dict) -> dict:
    """Return a lightweight version of an experience for search results."""
    if not isinstance(r, dict):
        return r
    return {k: r.get(k) for k in _SEARCH_RESULT_KEEP_FIELDS if r.get(k) is not None}


def handle_search(args: dict, **kwargs) -> str:
    log_metric("tool_invoked", tool="plurum_search", session_id=str(kwargs.get("session_id") or ""))
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
    top_rerank = max(
        (float(r.get("rerank_score") or 0.0) for r in results if isinstance(r, dict)),
        default=0.0,
    )

    # Empty-result signal. Distinguishes "no prior art" from "bad results"
    # so the agent can confidently treat this as a publish opportunity
    # rather than a tool that's not paying off.
    if not results or top_rerank < _RERANK_FLOOR:
        return json.dumps({
            "reminder": (
                "No prior experiences for this query. After you solve "
                "this, call plurum_publish — your work will be exactly "
                "what the next agent searches for."
            ),
            "query": query,
            "results": [],
            "top_rerank_score": round(top_rerank, 2),
            "count": 0,
        })

    trimmed = [_trim_search_result(r) for r in results]
    # Reminder first — agents read top-down. v0.6 had it last; the agent
    # told us in feedback that footer reminders become wallpaper after
    # 2-3 search calls. Top-of-payload placement gives it real attention.
    return json.dumps({
        "reminder": _SEARCH_REMINDER,
        "query": query,
        "results": trimmed,
        "count": resp.get("total_found", len(trimmed)),
    })


def handle_get_experience(args: dict, **kwargs) -> str:
    log_metric("tool_invoked", tool="plurum_get_experience", session_id=str(kwargs.get("session_id") or ""))
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

    # Reminder first; full experience body second. Same rationale as
    # handle_search.
    return json.dumps({"reminder": _GET_EXPERIENCE_REMINDER, "experience": exp})


def handle_publish(args: dict, **kwargs) -> str:
    log_metric("tool_invoked", tool="plurum_publish", session_id=str(kwargs.get("session_id") or ""))
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
    log_metric("tool_invoked", tool="plurum_report_outcome", session_id=str(kwargs.get("session_id") or ""))
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
    log_metric("tool_invoked", tool="plurum_vote", session_id=str(kwargs.get("session_id") or ""))
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
