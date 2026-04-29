"""Plurum Hermes plugin — collective knowledge for AI agents.

Pure standalone plugin. No memory slot squatting, no per-user fact
extraction. Five tools that let the agent search, drill into, contribute
to, and rate experiences in the Plurum collective.

Install (Git):
    hermes plugins install dunelabsco/plurum-hermes
    hermes plugins enable plurum

Configure:
    PLURUM_API_KEY in ~/.hermes/.env  (get one at https://plurum.ai)
    Optional: PLURUM_API_URL to point at a self-hosted instance.

Or run `hermes memory setup` and pick `plurum` if Hermes' generic
plugin-config wizard surfaces this plugin's schema.
"""

from __future__ import annotations

import logging

from .client import save_config  # noqa: F401  (re-exported for setup wizard)
from .hook import pre_llm_call as _pre_llm_call_handler
from .tools import TOOLS, _check_available

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hermes setup-wizard schema (optional but professional)
# ---------------------------------------------------------------------------

def get_config_schema():
    """Schema consumed by `hermes memory setup`-style wizards.

    Returning a list of fields lets Hermes prompt the user interactively
    instead of forcing them to edit ~/.hermes/.env by hand.
    """
    return [
        {
            "key": "api_key",
            "description": "Plurum agent API key",
            "secret": True,
            "required": True,
            "env_var": "PLURUM_API_KEY",
            "url": "https://plurum.ai",
        },
        {
            "key": "api_url",
            "description": "API base URL (advanced — leave default unless self-hosting)",
            "default": "https://api.plurum.ai",
            "env_var": "PLURUM_API_URL",
        },
    ]


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register all 5 tools and the auto-inject hook.

    Called once at plugin load. Each tool is gated by `_check_available`
    so when the user has no API key, the tools still appear in
    `hermes tools` (so they can see Plurum is installed) but the runtime
    check prevents dispatch and surfaces a clear setup hint.

    The pre_llm_call hook fires before every user turn. It decides
    whether the message is a task that might benefit from the
    collective, searches Plurum (300ms hard deadline), and injects
    relevant titles + ids as a `<plurum_context>` block alongside the
    user message. All failures are silent — the agent's normal flow is
    never blocked by the hook.
    """
    for name, schema, handler, emoji in TOOLS:
        ctx.register_tool(
            name=name,
            toolset="plurum",
            schema=schema,
            handler=handler,
            check_fn=_check_available,
            emoji=emoji,
        )

    ctx.register_hook("pre_llm_call", _pre_llm_call_handler)

    logger.info(
        "Plurum plugin registered (%d tools + pre_llm_call auto-inject)",
        len(TOOLS),
    )
