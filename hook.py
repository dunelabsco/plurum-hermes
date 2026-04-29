"""pre_llm_call auto-inject hook for the Plurum Hermes plugin.

Without this hook, agents tend to default to familiar tools
(browser_navigate, etc.) even when the tool description is aggressive
— verified live on the Hermes test box: a brand-new user gets
browser-first behavior unless they manually add a Plurum directive to
their user-profile memory. The hook turns "search the collective before
doing fresh research" from a suggestion in the tool description into
automatic behavior, without requiring user-side configuration.

Flow per user turn:
  1. Gate: is this a task that might have a Plurum match?
     - If OPENAI_API_KEY is available: tiny gpt-4o-mini classifier
       (~200ms, ~$0.00001/turn) returns is_task + a cleaned search query.
     - Otherwise: verb-match fallback (free, weaker).
  2. Search: call /api/v1/experiences/search with the cleaned query.
     Hard 300ms deadline via threading; agent never blocks.
  3. Filter: drop results below trust 0.4 / similarity 0.3 floors.
     If 0 survive → inject nothing.
  4. Format: render up to 3 titles + ids + trust scores as a compact
     <plurum_context> block.
  5. Return {"context": "..."} for Hermes to inject ahead of the model.

All failures silent. The agent's normal flow is never blocked. A circuit
breaker on the shared client keeps a downed backend from hammering the
hook loop.

Metrics are written to ~/.hermes/plurum-metrics.jsonl so we can audit
the funnel locally:
  considered → fired → search_done → had_results → injected → drill_in
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional, Tuple

from .client import PlurumClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables — exposed as constants so the hook is easy to reason about.
# ---------------------------------------------------------------------------

# Hard deadline for the search call. If we don't have results within this
# window, we skip injection for this turn and let the model proceed.
# Set generously: the Plurum search RPC routinely takes 1-3s because of
# the LLM cross-encoder reranker. Originally 300ms — that was based on a
# misread of the Hermes agent's "user freeze threshold" (which is total
# added latency, not single-call API latency). Live testing showed every
# call missing 300ms by ~2ms.
_SEARCH_DEADLINE_MS = 5000

# How many experiences to retrieve per search before filtering.
_SEARCH_LIMIT = 5

# Quality floors. Anything below either threshold is treated as noise
# and not injected.
_MIN_TRUST = 0.4
_MIN_SIMILARITY = 0.3

# How many surviving experiences to actually inject.
_INJECT_TOP_K = 3

# How long to remember a search result within a session before re-firing
# for the same query. Doesn't cross sessions — per-session is enough.
_SESSION_CACHE_TTL_SECS = 600

# LLM gate config. The gate is one tiny gpt-4o-mini call that decides
# is_task and returns a cleaned search query. Falls back to verb-match
# if OPENAI_API_KEY isn't set.
_GATE_MODEL = "gpt-4o-mini"
_GATE_DEADLINE_MS = 2000
_GATE_PROMPT = (
    "Decide whether the user's message is a task that might benefit from "
    "the prior experience of OTHER AI agents (research, comparison "
    "shopping, debugging, deployment, how-to, scraping, building "
    "something, fixing something, finding the cheapest/best/fastest "
    "option). Skip user-specific tasks — files / photos / conversations / "
    "personal data only THIS user has access to.\n\n"
    "Return JSON only:\n"
    '{"is_task": true|false, "search_query": "<cleaned query>" | null}\n\n'
    "If is_task is false, search_query must be null. If is_task is true, "
    "search_query is a clean, terse search phrase suitable for a "
    "vector-search index — strip pleasantries and emotion. Max 80 chars."
)

# Verb-match fallback when no OpenAI key is available. English-only by
# design; we log misses so we can add more languages once we have data.
_TASK_VERBS = re.compile(
    r"\b("
    r"find|look up|search|compare|buy|order|install|set up|setup|"
    r"configure|deploy|build|fix|debug|troubleshoot|write|generate|"
    r"create|make|book|plan|migrate|convert|optimize|research|"
    r"how do i|how can i|what(?:'s| is) the best|which is|where can i|"
    r"cheapest|fastest|easiest|recommend|should i use"
    r")\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Per-session state
# ---------------------------------------------------------------------------

_session_cache: dict[str, dict[str, Any]] = {}
_session_lock = threading.Lock()


def _cache_get(session_id: str, query_key: str) -> Optional[list[dict]]:
    with _session_lock:
        bucket = _session_cache.get(session_id) or {}
        entry = bucket.get(query_key)
        if not entry:
            return None
        if time.monotonic() > entry["expires"]:
            return None
        return entry["results"]


def _cache_put(session_id: str, query_key: str, results: list[dict]) -> None:
    with _session_lock:
        bucket = _session_cache.setdefault(session_id, {})
        bucket[query_key] = {
            "results": results,
            "expires": time.monotonic() + _SESSION_CACHE_TTL_SECS,
        }


# ---------------------------------------------------------------------------
# Metrics — local JSONL, not phoned home.
# ---------------------------------------------------------------------------

def _metrics_path() -> Optional[Path]:
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "plurum-metrics.jsonl"
    except Exception:
        return None


def _log_metric(event: str, **fields: Any) -> None:
    """Append a metric event to ~/.hermes/plurum-metrics.jsonl. Best-effort
    — never raises. Used to audit the funnel locally."""
    path = _metrics_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "event": event, **fields}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Gate — decide if this user message is a task
# ---------------------------------------------------------------------------

def _gate_via_llm(user_message: str) -> Optional[Tuple[bool, Optional[str]]]:
    """Single gpt-4o-mini call. Returns (is_task, cleaned_query) or None
    if the gate isn't available. Hard-deadlined; runs in the calling
    thread because we already enforce a budget at the hook level."""
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return None
    try:
        from urllib.request import Request, urlopen
    except Exception:
        return None

    body = {
        "model": _GATE_MODEL,
        "messages": [
            {"role": "system", "content": _GATE_PROMPT},
            {"role": "user", "content": user_message[:1500]},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "max_tokens": 120,
    }
    req = Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=_GATE_DEADLINE_MS / 1000.0) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        logger.debug("plurum gate call failed: %s", e)
        return None

    try:
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except Exception:
        return None

    is_task = bool(parsed.get("is_task"))
    query = parsed.get("search_query")
    if not isinstance(query, str) or not query.strip():
        query = None
    return is_task, (query.strip()[:200] if query else None)


def _gate_via_verbs(user_message: str) -> Tuple[bool, Optional[str]]:
    """Cheap fallback. English-only, brittle, but better than nothing
    when OPENAI_API_KEY isn't set."""
    if len(user_message.split()) < 5:
        return False, None
    if not _TASK_VERBS.search(user_message):
        return False, None
    # No clean-up step; use the raw message. Truncate.
    return True, user_message.strip()[:200]


def _gate(user_message: str) -> Tuple[bool, Optional[str]]:
    if not user_message or not user_message.strip():
        return False, None
    llm_result = _gate_via_llm(user_message)
    if llm_result is not None:
        return llm_result
    return _gate_via_verbs(user_message)


# ---------------------------------------------------------------------------
# Search — runs in a worker thread with a hard deadline
# ---------------------------------------------------------------------------

def _search_with_deadline(
    client: PlurumClient,
    query: str,
    deadline_ms: int,
) -> Optional[list[dict]]:
    """Spawn a thread to search Plurum. Wait up to `deadline_ms`. If the
    search returns in time, return the raw result list. Otherwise return
    None — the hook treats that as "skip this turn, agent proceeds"."""
    result_box: dict[str, Any] = {"results": None, "error": None}

    def _worker():
        try:
            resp = client.search_experiences(query, limit=_SEARCH_LIMIT)
            result_box["results"] = (resp or {}).get("results") or []
            client._record_success()
        except Exception as e:
            result_box["error"] = e
            client._record_failure()

    t = threading.Thread(target=_worker, daemon=True, name="plurum-hook-search")
    t.start()
    t.join(timeout=deadline_ms / 1000.0)
    if t.is_alive():
        # Search still running — abandon for this turn. Thread will keep
        # going in the background and finish eventually; nothing depends
        # on its result after this.
        return None
    if result_box["error"] is not None:
        return None
    return result_box["results"] or []


# ---------------------------------------------------------------------------
# Format — render the surviving results as <plurum_context>
# ---------------------------------------------------------------------------

def _filter_and_format(results: list[dict]) -> Tuple[str, list[str]]:
    """Apply trust + similarity floors, take top-K, render the context
    block. Returns (block_text, [experience_ids]). Empty block_text
    means nothing survived."""
    survivors: list[dict] = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        trust = float(r.get("trust_score") or r.get("rerank_score") or 0.0)
        sim = float(r.get("similarity") or 0.0)
        # Backend returns trust on a 0-1 scale; rerank_score is 0-10. If
        # the high signal looks like rerank_score, normalize.
        if trust > 1.5:
            trust = trust / 10.0
        if trust < _MIN_TRUST:
            continue
        if sim and sim < _MIN_SIMILARITY:
            continue
        survivors.append(r)
        if len(survivors) >= _INJECT_TOP_K:
            break

    if not survivors:
        return "", []

    lines = [
        "<plurum_context>",
        f"{len(survivors)} relevant Plurum experience(s) — call "
        "plurum_get_experience with an id to drill in, plurum_report_outcome "
        "after acting on one.",
        "",
    ]
    ids: list[str] = []
    for r in survivors:
        ident = r.get("short_id") or r.get("id") or ""
        title = (r.get("goal") or r.get("title") or "").strip().replace("\n", " ")
        trust = float(r.get("trust_score") or r.get("rerank_score") or 0.0)
        if trust > 1.5:
            trust = trust / 10.0
        ids.append(str(ident))
        lines.append(f"[{ident}] trust {trust:.2f} · {title[:200]}")
    lines.append("</plurum_context>")
    return "\n".join(lines), ids


# ---------------------------------------------------------------------------
# Public entry — registered as the pre_llm_call hook
# ---------------------------------------------------------------------------

def pre_llm_call(**kwargs: Any) -> Optional[dict]:
    """Hook callback. Hermes calls this before each LLM round. Return
    {"context": "..."} to inject text alongside the user message; return
    None for a silent no-op.

    Receives at least:
      session_id, user_message, conversation_history, is_first_turn,
      model, platform, sender_id
    """
    user_message = (kwargs.get("user_message") or "").strip()
    session_id = str(kwargs.get("session_id") or "")
    if not user_message:
        return None

    # Cheap pre-filter: don't even consider a turn whose first LLM round
    # wouldn't make sense for a task search. is_first_turn isn't reliable
    # across all Hermes flows; rely on the gate instead.
    _log_metric("considered", session_id=session_id, msg_len=len(user_message))

    # Initialize the client. If no API key, plugin is inert — silently
    # skip instead of erroring, exactly like the tools do.
    client = PlurumClient()
    if not client.has_api_key:
        _log_metric("skipped_no_key", session_id=session_id)
        return None
    if client.is_breaker_open():
        _log_metric("skipped_breaker_open", session_id=session_id)
        return None

    # Gate
    gate_t0 = time.monotonic()
    is_task, query = _gate(user_message)
    gate_ms = int((time.monotonic() - gate_t0) * 1000)
    if not is_task or not query:
        _log_metric(
            "gate_skip", session_id=session_id, gate_ms=gate_ms,
            reason="not_a_task" if not is_task else "no_query",
        )
        return None

    # Cache key on the cleaned query (paraphrases hash the same).
    cache_key = hashlib.md5(query.lower().encode("utf-8")).hexdigest()
    cached = _cache_get(session_id, cache_key)
    if cached is not None:
        block, ids = _filter_and_format(cached)
        _log_metric(
            "cache_hit", session_id=session_id, query=query,
            had_results=bool(ids), ids=ids,
        )
        return {"context": block} if block else None

    _log_metric("fired", session_id=session_id, query=query, gate_ms=gate_ms)

    # Search with hard deadline
    search_t0 = time.monotonic()
    results = _search_with_deadline(client, query, _SEARCH_DEADLINE_MS)
    search_ms = int((time.monotonic() - search_t0) * 1000)
    if results is None:
        _log_metric(
            "search_timeout", session_id=session_id, query=query,
            search_ms=search_ms, deadline_ms=_SEARCH_DEADLINE_MS,
        )
        return None

    _cache_put(session_id, cache_key, results)
    _log_metric(
        "search_done", session_id=session_id, query=query,
        search_ms=search_ms, raw_results=len(results),
    )

    # Format + filter
    block, ids = _filter_and_format(results)
    if not block:
        _log_metric(
            "no_survivors", session_id=session_id, query=query,
            raw_results=len(results),
        )
        return None

    _log_metric(
        "injected", session_id=session_id, query=query, ids=ids,
    )
    return {"context": block}
