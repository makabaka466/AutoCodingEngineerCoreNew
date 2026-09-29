"""Load the always-on incident workflow rules shipped with the application."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=2)
def load_incident_workflow_rules(profile: str = "full") -> str:
    """Return the incident-only Markdown policy, independent of project knowledge."""

    resource = files("autocoding_agent.incident").joinpath(
        "prompts", "incident_workflow.md"
    )
    content = resource.read_text(encoding="utf-8").strip()
    if not content:
        raise RuntimeError("The bundled incident workflow rules are empty.")
    if profile == "diagnosis":
        start = content.find("## 1. Assess the user's conversational page evidence first")
        end = content.find("## 4. Trace the smallest relevant code path")
        if start >= 0 and end > start:
            return (
                content[:start]
                + "## Previously checked page identity\n"
                "The host has accepted a page with relative source paths and has returned current "
                "business-query evidence. This is not proof of the cause. Check the latest user "
                "intent, title/path, screenshot context, mapping and current source still agree. "
                "Never treat a record ID or an error alone as page identity. Preserve independent "
                "vendor/product and function clues; a generic fuzzy match is not an alias. "
                "A mapping URL is only a clue; source identity must match. Put evidence in "
                "matched_evidence and mismatches in unresolved_conflicts. For a changed, denied or "
                "conflicting page, stop business diagnosis and ask one confirmation question or "
                "request a bounded page_lookup using selected project semantics, at most 20 rows. "
                "Never scan all pages or hide identity conflicts in a positive explanation. "
                "Missing logs/schema/deployment or uncertain causes are diagnostic gaps, not page "
                "conflicts; do not reconfirm an already supported page merely for those gaps.\n\n"
                + content[end:]
            )
    return content
