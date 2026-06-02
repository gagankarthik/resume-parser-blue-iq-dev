"""
OpenAI GPT-4o structured output parser.

Retry strategy:
  • MAX_RETRIES=3 total attempts
  • RateLimitError → exponential backoff with ±20% jitter (prevents thundering herd)
    Delays: ~5s, ~10s, ~20s (before jitter)
  • Other errors → 1s pause then retry; raise AIParsingError after exhaustion

Token safety:
  Sections are truncated to MAX_SECTION_CHARS to stay within max_tokens budget.
  Each section gets an equal share of the character budget.
"""

import asyncio
import json
import random

from openai import AsyncOpenAI, RateLimitError

from app.core.config import get_settings
from app.core.exceptions import AIParsingError
from app.core.logging import get_logger
from app.models.schemas import ParsedResumeAI
from app.services.parsing.rule_parser import RuleExtracted

log = get_logger(__name__)

MAX_RETRIES       = 3
BACKOFF_BASE      = 5      # seconds
JITTER_FACTOR     = 0.2    # ±20 % of backoff delay
MAX_SECTION_CHARS = 8_000  # per section — prevents token overflow for long resumes
MAX_TOTAL_CHARS   = 24_000 # total prompt cap (≈ 6K tokens at ~4 chars/token)


def _truncate_sections(sections: dict[str, str]) -> dict[str, str]:
    """Cap each section and the total to avoid hitting max_tokens."""
    truncated: dict[str, str] = {}
    total = 0
    for key, text in sections.items():
        chunk = text[:MAX_SECTION_CHARS]
        if total + len(chunk) > MAX_TOTAL_CHARS:
            chunk = chunk[: MAX_TOTAL_CHARS - total]
        truncated[key] = chunk
        total += len(chunk)
        if total >= MAX_TOTAL_CHARS:
            break
    return truncated


def _build_prompt(sections: dict[str, str], anchors: RuleExtracted) -> str:
    anchors_block = json.dumps(
        {
            "emails":          anchors.emails,
            "phones":          anchors.phones,
            "linkedin_urls":   anchors.linkedin_urls,
            "github_urls":     anchors.github_urls,
            "portfolio_urls":  anchors.portfolio_urls,
        },
        indent=2,
    )

    safe_sections = _truncate_sections(sections)
    sections_block = "\n\n".join(
        f"=== {k.upper()} ===\n{v}"
        for k, v in safe_sections.items()
        if v.strip()
    )

    return f"""You are an expert healthcare resume parser specialising in nursing and allied health professions.
Extract structured information from the resume text below.

EXTRACTION RULES:
- Extract ONLY what is explicitly stated. Never infer or hallucinate.
- Use null for any field not present in the text.
- Dates: ISO format YYYY-MM, or "Present" for current roles.
- Separate each role/assignment as its own experience entry (important for travel nurses).
- Skills: individual items only — not sentences. Include clinical specialties AND credentials separately.
- Certifications (BLS, ACLS, PALS, CCRN, CEN, NRP, TNCC, OCN…) → certifications[] not skills[].
- Preserve credential abbreviations exactly (RN, LPN, CRT, RRT, OT, PT, SLP…). Do NOT expand them.
- Float pool / per-diem / agency assignments: list each separately in experience[].
- Multi-column resumes: text may be extracted left-column-first; treat it as sequential.

HEALTHCARE CREDENTIAL ABBREVIATIONS — preserve as-is in output:
  Nursing:     RN, LPN, LVN, CNA, CRNA, NP, RNFA, MSN, BSN
  Respiratory: CRT, RRT
  Therapy:     OT, COTA, PT, PTA, SLP, SLPA
  Social Work: CSW, LCSW, LICSW, LMSW, MSW
  Imaging:     CT Tech, MRI Tech, X-Ray Tech, Echo Tech, EKG Tech
  Surgical:    OR Tech, CST, SPT, CVOR Tech

PRE-EXTRACTED CONTACT ANCHORS — use these values directly, do not re-extract:
{anchors_block}

RESUME TEXT:
{sections_block}
"""


async def parse(
    sections: dict[str, str],
    anchors: RuleExtracted,
) -> tuple[ParsedResumeAI, int]:
    """
    Parse resume with GPT-4o structured output.
    Returns (parsed_resume, total_tokens_used).
    Raises AIParsingError after MAX_RETRIES exhausted.
    """
    settings = get_settings()
    client   = AsyncOpenAI(api_key=settings.openai_api_key)
    prompt   = _build_prompt(sections, anchors)
    last_exc: Exception | None = None

    for attempt in range(MAX_RETRIES):
        try:
            response = await client.beta.chat.completions.parse(
                model=settings.openai_model,
                max_tokens=settings.openai_max_tokens,
                temperature=0.0,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a precise healthcare resume parser. "
                            "Output valid JSON exactly matching the requested schema. "
                            "Every list field must be a JSON array, never null."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format=ParsedResumeAI,
            )

            parsed = response.choices[0].message.parsed
            tokens = response.usage.total_tokens if response.usage else 0

            if parsed is None:
                raise AIParsingError("Model returned empty structured output")

            log.info("ai_parse_success", attempt=attempt + 1, tokens=tokens)
            return parsed, tokens

        except RateLimitError as exc:
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                base  = BACKOFF_BASE * (2 ** attempt)
                jitter = base * JITTER_FACTOR * (2 * random.random() - 1)
                wait  = max(base + jitter, 1.0)
                log.warning("ai_rate_limited", attempt=attempt + 1, retry_in=round(wait, 1))
                await asyncio.sleep(wait)
            else:
                raise AIParsingError(
                    f"OpenAI rate limit exceeded after {MAX_RETRIES} attempts"
                ) from exc

        except AIParsingError:
            raise

        except Exception as exc:
            last_exc = exc
            log.warning("ai_attempt_failed", attempt=attempt + 1, error=str(exc))
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(1)
            else:
                raise AIParsingError(
                    f"AI parsing failed after {MAX_RETRIES} attempts: {exc}"
                ) from exc

    raise AIParsingError(f"AI parsing failed: {last_exc}")
