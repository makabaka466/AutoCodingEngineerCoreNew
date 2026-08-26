"""Load the always-on incident workflow rules shipped with the application."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=1)
def load_incident_workflow_rules() -> str:
    """Return the incident-only Markdown policy, independent of project knowledge."""

    resource = files("autocoding_agent.incident").joinpath(
        "prompts", "incident_workflow.md"
    )
    content = resource.read_text(encoding="utf-8").strip()
    if not content:
        raise RuntimeError("The bundled incident workflow rules are empty.")
    return content
