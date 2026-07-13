"""
OpenAI (gpt-4.1-mini) structured-output parser.

Retry strategy:
  * MAX_RETRIES=3 total attempts
  * RateLimitError -> exponential backoff with ±20% jitter (prevents thundering herd)
    Delays before the two retries: ~5s, ~10s (before jitter); the 3rd attempt raises
  * Other errors -> 1s pause then retry; raise AIParsingError after exhaustion

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
# Input caps. gpt-4.1-mini has a very large context window, so the constraint is
# cost/latency, not the model - keep these generous so a long resume's later
# sections (more work history, education, references) are never truncated away
# before the model sees them. ~60K chars ≈ 15K tokens, comfortably inside context.
MAX_SECTION_CHARS = 20_000  # per section
MAX_TOTAL_CHARS   = 60_000  # total résumé text

# One client per event loop - connection-pool reuse across parses, rebuilt when the
# running loop changes (same pattern/rationale as the multi-agent BaseAgent). The
# worker Lambda creates a fresh loop per invocation, so a client cached from a
# previous (now-closed) loop would fail on warm-container reuse.
_client: AsyncOpenAI | None = None
_bound_loop: asyncio.AbstractEventLoop | None = None


def _get_client() -> AsyncOpenAI:
    global _client, _bound_loop
    loop = asyncio.get_running_loop()
    if _client is None or _bound_loop is not loop:
        _client = AsyncOpenAI(api_key=get_settings().openai_api_key)
        _bound_loop = loop
    return _client


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

    return f"""You are an expert healthcare resume parser. You handle EVERY healthcare profession
equally well — registered/licensed nurses AND allied health: radiologic / CT / MRI / mammography
technologists, respiratory therapists, OT/PT/SLP, surgical techs, lab/imaging, social work, etc.
This is NOT a nurse-only schema: parse a Radiologic Technologist, a Respiratory Therapist, or a
CT/Mammography Tech with the same fields you would use for an RN (profession, specialties, licenses).
NEVER return an empty result just because the candidate is not a nurse.
Extract structured information from the resume text below.

EXTRACTION RULES:
- Extract ONLY what is explicitly stated. NEVER infer, guess, expand, or hallucinate. If a value is not written on the résumé, use null.

ATTRIBUTION — do NOT smear SUMMARY facts across jobs (critical for healthcare résumés):
- FACILITY ATTRIBUTES stated ONLY in the professional summary/objective (e.g. "experience in a 64-bed ICU", "3:1 ratio", "Level I trauma center", "Magnet facility") describe the candidate in general. Attach such a fact to a SPECIFIC experience[] entry ONLY when the résumé states it inside THAT role's own block/heading/bullets.
- If it is ambiguous because the candidate worked at MULTIPLE facilities and it is not clearly tied to one, LEAVE that role-level field null: facility_beds, beds_in_unit, nurse_to_patient_ratio, trauma_level, service_type, teaching/magnet/trauma flags. Do NOT copy it onto every role and do NOT pick one arbitrarily. (Example: summary says "64 bed unit" and the person worked at 3 hospitals → every role's facility_beds is null.)
- This rule does NOT apply to a role's OWN identity. ALWAYS extract `profession` and the role's `specialties` from that role's title/heading — e.g. role "Critical Care Registered Nurse" → profession "RN"; role "Neonatal Intensive Registered Nurse" → profession "RN". Never leave profession null when the role title states or implies it. Only a specialty that would have to be mined from the SUMMARY or from unrelated duty bullets should be dropped.

EXPLAINABILITY — extraction_notes[]:
- Whenever you deliberately LEAVE A FIELD NULL because a fact was ambiguous (per the attribution rule above), OR you attach a fact to a role that was not obvious, add an entry to extraction_notes: {{"field": "<dotted path e.g. experience[1].facility_beds>", "value": <the value or null>, "confidence": <0.0-1.0>, "reason": "<short plain-language why>"}}.
- `confidence` is how sure you are of THE DECISION, not of a value. A confident, evidence-backed decision to LEAVE A FIELD NULL is HIGH (0.85-0.95) — you are sure it should be null. Use a LOWER value (0.3-0.6) only for a genuine judgment call. NEVER default confidence to 0.
- Example: summary says "worked in 64-bed units" and there are 3 hospitals → for each such role add {{"field":"experience[0].facility_beds","value":null,"confidence":0.9,"reason":"'64 beds' stated only in the summary; 3 facilities listed, cannot attribute to one"}}.
- Keep notes brief and only for genuine decisions. An empty list is correct when nothing was ambiguous. NEVER invent data just to fill a note.
- Use null for any field not present in the text.
- full_name: the candidate's name ONLY. Do NOT include trailing credential, licence, or degree suffixes (e.g. "Jane Smith, RN BSN" → "Jane Smith").
- personal_info.credentials: the post-nominal credentials that follow the name (e.g. "Jane Smith, RN, BSN, MPH, CCRN" → ["RN", "BSN", "MPH", "CCRN"]), each as a SEPARATE item in the order written. These are stripped from full_name and MUST be captured here — never drop them. Post-nominals live ONLY here: do NOT copy them into skills[], and NEVER fabricate a certifications[] or licenses[] entry from a post-nominal alone — only from items the résumé actually lists.
- Dates — keep exactly the precision written; never invent a missing day or month:
  • Full date  → MM/DD/YYYY   (e.g. "2/16/2024" → "02/16/2024", "July 21, 2019" → "07/21/2019")
  • Month+year → MM/YYYY      (e.g. "August 2018" → "08/2018", "9/2019" → "09/2019") — do NOT add a day.
  • Year only  → YYYY
  • Current role → "Present".
  For a range like "August 2018 - April 19", output start "08/2018" and end "04/2019" (do NOT fabricate days).
- description: an ARRAY where each item is ONE bullet/line copied as written. If a single bullet contains multiple sentences, keep them together in that one item — do NOT split a bullet into several items, and do NOT merge separate bullets.
- Separate each role/assignment as its own experience entry (important for travel nurses).
- TRAVEL / AGENCY ASSIGNMENTS (critical — do not flatten): when a single travel or agency role lists several facilities/sites underneath it (often indented bullets under one heading like "Travel RN — Supplemental Healthcare"), output ONE experience entry PER facility. Each such entry MUST carry the SAME profession/role as the umbrella heading (e.g. role "Travel RN", profession "RN") and set agency_name to the staffing agency. NEVER emit a facility as an entry with role "Unknown" or a blank role — if a sub-site has no title of its own, inherit the umbrella role. Keep each facility's own city/state/dates.
- For each experience entry, also fill these ONLY when explicitly stated (else null/empty — never guess or infer):
  • location — the FULL address line exactly as written, including street/suite/number if present (e.g. "135 Brush Hill Road, Milton, MA 02186"). Copy the WHOLE line. Do NOT drop the street and keep only city/state/zip.
  • city, state, country, zip_code — copy the parts that appear, verbatim. Keep state as written ("NY", "VA" — do NOT expand to "New York"/"Virginia"). Leave country null unless the résumé literally names a country (do NOT assume "United States"). Never guess a ZIP from the city.
  • employer_phone — the employer/facility phone number written next to that role (verbatim, e.g. "304-287-2120"). Null if not stated. NEVER use the candidate's own contact phone here.
  • profession — the credential for that role as written (e.g. "RN", "LPN", "CRT"); do NOT expand it.
  • specialties — ONLY the unit/specialty named in that role's heading/title or an explicit unit label (e.g. "Med Surg/Tele", "ICU"). Return a list of OBJECTS, one per specialty, each with just the name: [{{"name": "Med Surg/Tele"}}, {{"name": "ICU"}}]. Fill ONLY `name` — leave specialty_id/confidence/group null; the system fills those. Do NOT mine phrases from duty bullets — equipment, therapies, patient populations, or physician groups mentioned in a bullet are NOT specialties.
  • position_held, agency_name, shift (Days/Nights/Rotating/Weekends), charting_system (Epic/Cerner/Meditech/PointClickCare…), reason_for_leaving.
  • employment_type — 'Full-time', 'Part-time', or 'PRN' (per diem) ONLY if stated for this role, else null.
  • patient_load — the patient COUNT if stated (e.g. "managed 6 patients" → "6"). A ratio like "1:4" goes in nurse_to_patient_ratio, not here.
  • nurse_to_patient_ratio, facility_beds, beds_in_unit, service_type, trauma_level, additional_info.
  • teaching_facility, magnet_facility, trauma_facility — only as "Yes"/"No"/"N/A" when the resume says so, else null.
- Skills: individual items only — not sentences. Clinical specialties, units, and competencies. Do NOT put certifications (CPR, BLS, ACLS, PALS…), licenses, driver's licenses, or academic degrees (BSN, MSN) in skills[] — those go in certifications[]/licenses[].
- NEVER drop an item listed under a credentials/certifications/licenses heading — each must land in skills[], certifications[], or licenses[].
- Certifications (BLS, ACLS, PALS, CCRN, CEN, NRP, TNCC, OCN…) → certifications[] not skills[]. Also keep NON-clinical credentials that are listed (e.g. "CPR", "First Aid", "CNA", "Driver's License") in certifications[] — do not discard them just because they are not clinical or not state licenses.
- A bare nursing/allied PRACTICE credential (RN, LPN, LVN) LISTED in a credentials/certifications/licenses section is a professional license even with no number/state — put it in licenses[] with license_type set to the credential and missing fields null, NOT in certifications[]. Post-nominals after the candidate's name alone do NOT create a license entry.
- Certification dates: a bare date next to a cert (e.g. "BLS: 12/2024") is AMBIGUOUS — do NOT assume it is an expiry. Put it in the neutral `date` field. "Completed/Issued/Awarded <date>" (e.g. "Steps to Leadership Completed December 2024") → `issued_date`, NEVER `expiry_date`. Use `expiry_date` only when labeled expires/valid through/renewal.
- Certifications are usually listed one name per line, sometimes with the issuing organization (e.g. "American Heart Association", "American Red Cross") on a nearby line. EVERY line that names a certification, license course, or training — e.g. "Advanced Life Support", "NIH Stroke Scale Certification", "Basic Life Support (BLS)" — is its OWN certifications[] entry. NEVER drop such a line and NEVER absorb it into an adjacent cert, even when it has no issuer or when an issuer/date sits beside a DIFFERENT cert. Two certification names on consecutive lines are TWO entries, not one. An issuer organization and date attach to the single cert they belong with; a cert with no issuer of its own keeps issuer/date null rather than borrowing a neighbour's.
- LICENSES vs certifications — a STATE professional license is NOT a certification; put it in licenses[], never certifications[] or skills only:
  • Any state RN/LPN/RT/etc. licence, e.g. "Florida RN License #RN9411204", "Active New York State Registered Nurse License", "Compact/Multistate RN License", "Radiologic Technologist License (TX)".
  • Capture: name (as written), license_type (the credential, e.g. "RN"/"RT" — do NOT expand), state (as written, keep "NY"), license_number (verbatim, INCLUDING any letter prefix like "RN9411204" — never drop the number), status ("Active"/"In progress"…), and issued/expiry dates only if stated.
  • Set is_compact=true ONLY if the résumé literally says compact/multistate/eNLC.
  • A licence in progress / pending (e.g. "MSN in progress", "license pending") is still captured with status reflecting that — never omit it.
- EDUCATION — populate education[] for EVERY degree/diploma; NEVER leave it empty when the résumé lists any schooling:
  • One entry per DEGREE with institution, degree, field_of_study, location, start_year, graduation_year, gpa — only when stated.
  • INSTITUTION INHERITANCE: when ONE school name is written once above MULTIPLE degrees/dates (e.g. "ECPI University" then "Associates in Nursing 2018" then "BSN 2019"), repeat that SAME institution on EACH degree entry; never leave it blank or emit "Unknown Institution", and do NOT create a separate degree-less entry just for the school header.
  • Capture any 4-digit year tied to the entry wherever it sits — end of the institution/degree line, a separate date column, or after "Graduated/Completed/Class of/Conferred/Expected". A year RANGE ("2013–2015") means start_year = first, graduation_year = last; a single year is the graduation_year. Output years as integers, never a month/day string.
  • A DEGREE belongs in education[] even when listed under a CERTIFICATIONS/LICENSES/CREDENTIALS heading — a diploma/degree is education, never a certification. Include in-progress/expected degrees (put the expected year in graduation_year only if given).
- Preserve credential abbreviations exactly (RN, LPN, CRT, RRT, OT, PT, SLP, RT(R), RT(CT), RT(M), ARRT…). Do NOT expand them.
- Float pool / per-diem / agency / travel assignments: list each as its OWN experience[] entry.
- personal_info.location: the candidate's FULL home address line exactly as written, including the street/number if present (e.g. "135 Brush Hill Road, Milton, MA 02186"). Do NOT shorten it to just city/state/zip.
- References: extract any listed referees into references[] (name, relationship/title, company, email, phone). Capture each referee's own credentials within their name/title if written (e.g. "Jane Doe, RN, BSN" — keep "RN, BSN"). If the resume only says "References available upon request", leave references[] empty.
- Awards/honors: extract each award, honor, or recognition into awards[] as a short string (include the year in parentheses if stated). Do NOT put awards in skills[] or experience[].
- Publications: extract each publication, poster, or research contribution into publications[] as a single citation string. Do NOT put publications in experience[] or projects[].
- Professional associations: society/association memberships, honor societies, committees, collaboratives, and process-owner roles go in professional_associations[] verbatim (e.g. "Sigma Theta Tau International Honor Society of Nursing Member", "Sepsis Clinical Services Committee", "SJHS Sepsis Process Owner"). They are NOT certifications, licenses, or skills — and never drop them.
- Academic honors ("Summa Cum Laude", "Magna Cum Laude") on a degree line go in awards[].
- Multi-column resumes: text may be extracted left-column-first; treat it as sequential.

HEALTHCARE CREDENTIAL ABBREVIATIONS — preserve as-is in output:
  Nursing:     RN, LPN, LVN, CNA, CRNA, NP, RNFA, MSN, BSN
  Respiratory: CRT, RRT
  Therapy:     OT, COTA, PT, PTA, SLP, SLPA
  Social Work: CSW, LCSW, LICSW, LMSW, MSW
  Imaging:     Rad Tech, RT(R), RT(CT), RT(M), RT(MR), ARRT, CT Tech, MRI Tech, Mammography Tech, X-Ray Tech, Echo Tech, EKG Tech, Sonographer, RDMS
  Surgical:    OR Tech, CST, SPT, CVOR Tech, Sterile Processing Tech

PRE-EXTRACTED CONTACT ANCHORS — regex-found, authoritative; use these values directly. But if a list is EMPTY, the regex found nothing (OCR may have garbled it): extract that field from the resume text yourself, repairing obvious OCR artifacts (stray spaces inside an email, '(@' for '@'). Never leave the email null when one is visible in the text.
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
    client   = _get_client()
    prompt   = _build_prompt(sections, anchors)
    last_exc: Exception | None = None

    for attempt in range(MAX_RETRIES):
        try:
            response = await client.beta.chat.completions.parse(
                model=settings.openai_model,
                max_tokens=settings.openai_max_tokens,
                temperature=0.0,
                seed=settings.openai_seed,
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
