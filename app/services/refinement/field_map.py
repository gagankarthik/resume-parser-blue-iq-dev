"""Map a corrected `changed_fields` path to the agent that produced it.

Feedback paths are the dotted/indexed leaf paths emitted by
`app.services.feedback.diff_fields`, e.g.::

    personal_info.full_name
    experience[0].description[2]
    certifications[1].name
    skills[3]

Refinement groups corrections by the responsible agent so each agent's prompt is
improved by exactly the mistakes it made. The mapping mirrors how the multi-agent
orchestrator assembles `ParsedResumeAI` (see
`app/services/parsing/orchestrator.py::parse`).
"""

from __future__ import annotations

# Agent display names - MUST match the `name` attribute on each agent class so the
# store can look up rules by the same key the agent calls with (`self.name`).
PERSONAL     = "PersonalInfoAgent"
WORK         = "WorkExperienceAgent"
EDUCATION    = "EducationAgent"
CREDENTIALS  = "CredentialsAgent"
SUPPLEMENTAL = "SupplementalAgent"

AGENT_NAMES: tuple[str, ...] = (PERSONAL, WORK, EDUCATION, CREDENTIALS, SUPPLEMENTAL)

# Top-level result key -> owning agent. Keys not listed here (e.g. derived
# `confidence`, or matcher-owned specialty ids) are intentionally excluded: a
# reviewer correcting them is not signal an agent prompt can act on.
_ROOT_TO_AGENT: dict[str, str] = {
    "personal_info":              PERSONAL,
    "experience":                 WORK,
    "education":                  EDUCATION,
    "skills":                     CREDENTIALS,
    "certifications":             CREDENTIALS,
    "licenses":                   CREDENTIALS,
    "professional_associations":  CREDENTIALS,
    "projects":                   SUPPLEMENTAL,
    "languages":                  SUPPLEMENTAL,
    "references":                 SUPPLEMENTAL,
    "awards":                     SUPPLEMENTAL,
    "publications":               SUPPLEMENTAL,
}


def _root_key(path: str) -> str:
    """The top-level result key of a leaf path (``experience[0].role`` -> ``experience``)."""
    head = path.split(".", 1)[0]
    # Strip any trailing list index: ``skills[3]`` -> ``skills``.
    bracket = head.find("[")
    return head[:bracket] if bracket != -1 else head


def agent_for_path(path: str) -> str | None:
    """Return the agent name that owns `path`, or None if no agent produces it."""
    if not path:
        return None
    return _ROOT_TO_AGENT.get(_root_key(path))
