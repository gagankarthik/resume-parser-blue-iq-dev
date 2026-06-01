"""
OpenAI GPT-4o structured output parser.

Uses the Pydantic response_format (beta.chat.completions.parse) to get
guaranteed schema-valid JSON from the model — no post-processing needed.

Strategy:
  1. Build a prompt with section-segmented text + pre-extracted anchors
  2. Parse with structured output (ParsedResumeAI schema)
  3. Retry once on failure with a simplified prompt
"""

import json
from typing import Any

from openai import AsyncOpenAI

from app.core.config import get_settings
from app.core.exceptions import AIParsingError
from app.core.logging import get_logger
from app.models.schemas import ParsedResumeAI
from app.services.parsing.rule_parser import RuleExtracted

log = get_logger(__name__)


def _build_prompt(sections: dict[str, str], anchors: RuleExtracted) -> str:
    anchors_block = json.dumps(
        {
            "emails": anchors.emails,
            "phones": anchors.phones,
            "linkedin_urls": anchors.linkedin_urls,
            "github_urls": anchors.github_urls,
            "portfolio_urls": anchors.portfolio_urls,
        },
        indent=2,
    )

    sections_block = "\n\n".join(
        f"=== {section.upper()} ===\n{content}"
        for section, content in sections.items()
        if content.strip()
    )

    return f"""You are an expert healthcare resume parser specializing in nursing and allied health professions.
Extract structured information from the resume text below.

IMPORTANT RULES:
- Extract ONLY what is explicitly stated. Do not infer or hallucinate.
- Use null for any field that is not present.
- Dates should be in ISO format (YYYY-MM) or "Present" for current roles.
- For experience, separate each role as its own entry even if at the same company.
- Skills should be individual items — include clinical specialties, certifications, and credentials separately.

HEALTHCARE-SPECIFIC EXTRACTION RULES:
- Credentials (RN, LPN, CNA, RRT, PT, OT, SLP, etc.) belong in the skills list.
- Clinical specialties (ICU, NICU, ER, OR, PACU, Med Surg, Telemetry, etc.) belong in the skills list.
- Unit abbreviations in experience roles should be preserved exactly as written (e.g. "RN - MICU", "CRT NICU").
  The normalization layer will expand them — do not expand abbreviations yourself.
- Certifications such as BLS, ACLS, PALS, NRP, TNCC, CEN, CCRN, OCN should be captured in certifications[].
- If a candidate lists multiple specialties or float pool experience, list each specialty separately in skills[].
- For travel nurses or agency staff, note each assignment as a separate experience entry.

COMMON HEALTHCARE CREDENTIAL ABBREVIATIONS (preserve in output as-is):
  Nursing: RN, LPN, LVN, CNA, CRNA, NP, RNFA
  Respiratory: CRT, RRT
  Therapy: OT, COTA, PT, PTA, SLP, SLPA
  Social Work: CSW, LCSW, LICSW, LMSW, MSW
  Imaging: CT Tech, MRI Tech, X-Ray Tech, Echo Tech, EKG Tech
  Surgical: OR Tech, CST, SPT, CVOR Tech

PRE-EXTRACTED ANCHORS (use these values directly):
{anchors_block}

RESUME TEXT:
{sections_block}
"""


async def parse(
    sections: dict[str, str],
    anchors: RuleExtracted,
) -> tuple[ParsedResumeAI, int]:
    """
    Returns (parsed_resume, tokens_used).
    Raises AIParsingError if both attempts fail.
    """
    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    prompt = _build_prompt(sections, anchors)

    for attempt in range(2):
        try:
            response = await client.beta.chat.completions.parse(
                model=settings.openai_model,
                max_tokens=settings.openai_max_tokens,
                temperature=0.0,  # deterministic output
                messages=[
                    {
                        "role": "system",
                        "content": "You are a precise resume parser that outputs valid structured JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format=ParsedResumeAI,
            )

            parsed = response.choices[0].message.parsed
            tokens = response.usage.total_tokens if response.usage else 0

            if parsed is None:
                raise AIParsingError("Model returned empty parsed output")

            log.info("ai_parse_success", attempt=attempt + 1, tokens=tokens)
            return parsed, tokens

        except AIParsingError:
            raise
        except Exception as exc:
            log.warning("ai_parse_attempt_failed", attempt=attempt + 1, error=str(exc))
            if attempt == 1:
                raise AIParsingError(f"AI parsing failed after 2 attempts: {exc}") from exc

    raise AIParsingError("Unreachable")
