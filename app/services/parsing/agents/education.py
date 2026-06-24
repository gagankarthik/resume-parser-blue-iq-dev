"""EducationAgent — degrees, institutions, fields, years."""

from __future__ import annotations

from app.models.schemas import EducationItem

from .base import BaseAgent, TokenMeter
from .prompts import CORE_RULES
from .schemas import EducationResult

_SYSTEM = f"""You extract the EDUCATION section of a healthcare résumé into {{ "education": [ ... ] }}.

{CORE_RULES}

RULES:
- One entry per institution/degree. Capture institution, degree, field_of_study, start_year, graduation_year, gpa — only when stated.
- YEARS: capture any 4-digit year tied to the entry, wherever it sits — at the end of the institution or degree line, in a separate date column, or after a label like "Graduated", "Completed", "Class of", "Conferred", or "Expected". A bare standalone year next to an education entry IS its graduation_year. Output years as integers (e.g. 2015), never as a month/day string.
- A YEAR RANGE ("2013–2015", "2013 to 2015", "Aug 2013 - May 2015") means start_year = the first year and graduation_year = the last year. A single year is the graduation_year (leave start_year null).
- A degree "in progress" (e.g. "MSN in progress / expected 2027") IS included; put the expected year in graduation_year only if a year is given, else leave it null. Never drop an in-progress degree.
- An academic DEGREE belongs here even when the résumé lists it under a different heading. Capture any degree (ADN, BSN, "Bachelors Degree in Science of Nursing", MSN, etc.) wherever it appears — including under a CERTIFICATIONS, LICENSES, or CREDENTIALS section — not only under an "Education" header. A diploma/degree is education, never a certification."""


class EducationAgent(BaseAgent):
    name = "EducationAgent"

    async def run(self, text: str, meter: TokenMeter) -> list[EducationItem]:
        user = f"=== RESUME TEXT ===\n{text}\n=== END ===\n\nReturn the education entries."
        result = await self._structured_call(_SYSTEM, user, EducationResult, meter, max_tokens=2048)
        return result.education
