"""
Per-field confidence scoring.

Scores reflect how complete and verifiable each section is.
Used by enterprise clients to flag records that need human review.

Score range: 0.0 – 1.0
"""

import re

from app.models.schemas import ConfidenceScores, ParsedResumeAI

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NON_DIGIT_RE = re.compile(r"\D")


def _has_phone_digits(value: str) -> bool:
    """True when the value holds a plausible phone digit count (7+).

    Counts TOTAL digits, not consecutive — formatted numbers like
    '(555) 234-5678' never have a 7-digit run but are valid.
    """
    return len(_NON_DIGIT_RE.sub("", value)) >= 7


def score(parsed: ParsedResumeAI) -> ConfidenceScores:
    personal   = _score_personal(parsed)
    experience = _score_experience(parsed)
    education  = _score_education(parsed)
    skills     = _score_skills(parsed)
    mapping    = _score_catalog_mapping(parsed)
    return ConfidenceScores(
        personal_info=personal,
        experience=experience,
        education=education,
        skills=skills,
        catalog_mapping=mapping,
        overall=_overall(personal, experience, education, skills, mapping),
    )


def _score_personal(parsed: ParsedResumeAI) -> float:
    p = parsed.personal_info
    points = 0.0
    total = 5.0

    if p.full_name and len(p.full_name.split()) >= 2:
        points += 1.0
    if p.email and _EMAIL_RE.match(p.email):
        points += 1.5
    if p.phone and _has_phone_digits(p.phone):
        points += 1.0
    if p.location:
        points += 0.5
    if p.linkedin_url or p.github_url:
        points += 1.0

    return round(min(points / total, 1.0), 2)


def _score_experience(parsed: ParsedResumeAI) -> float:
    if not parsed.experience:
        return 0.0

    points_per_entry = []
    for exp in parsed.experience:
        p = 0.0
        if exp.company:
            p += 0.3
        if exp.role:
            p += 0.3
        if exp.start_date:
            p += 0.2
        if exp.description or exp.achievements:
            p += 0.2
        points_per_entry.append(p)

    return round(sum(points_per_entry) / len(points_per_entry), 2)


def _score_education(parsed: ParsedResumeAI) -> float:
    if not parsed.education:
        return 0.0

    points_per_entry = []
    for edu in parsed.education:
        p = 0.0
        if edu.institution:
            p += 0.4
        if edu.degree:
            p += 0.3
        if edu.graduation_year:
            p += 0.3
        points_per_entry.append(p)

    return round(sum(points_per_entry) / len(points_per_entry), 2)


def _score_skills(parsed: ParsedResumeAI) -> float:
    count = len(parsed.skills)
    if count == 0:
        return 0.0
    if count >= 5:
        return 1.0
    return round(count / 5, 2)


def _score_catalog_mapping(parsed: ParsedResumeAI) -> float:
    """Mean match confidence of the role entities resolved to platform catalog ids.

    Reflects how confidently a résumé's structured entities mapped to Gig ids —
    the "accuracy" dimension of the parse. For each role, every entity whose SOURCE
    field is present contributes its match confidence (0.0 when it went unmatched):
    profession, facility (a real company, not the "Unknown" placeholder), country,
    state, and each specialty. City is counted only when it actually resolved to an
    id (its lookup is a best-effort live search that may be disabled or unreachable,
    so an absent city_id must not be read as a mapping failure). 0.0 when there is
    nothing to map (no experience / no mappable entities).
    """
    vals: list[float] = []
    for exp in parsed.experience:
        if exp.profession:
            vals.append(exp.profession_confidence)
        if exp.company and exp.company != "Unknown":
            vals.append(exp.facility_confidence)
        if exp.country:
            vals.append(exp.country_confidence)
        if exp.state:
            vals.append(exp.state_confidence)
        if exp.city and exp.city_id is not None:
            vals.append(exp.city_confidence)
        for sm in exp.specialties:
            vals.append(sm.confidence)
    if not vals:
        return 0.0
    return round(sum(vals) / len(vals), 2)


def _overall(
    personal: float, experience: float, education: float, skills: float, mapping: float
) -> float:
    # Takes the already-computed sub-scores (score() has them) instead of
    # recomputing them — same result, less work. catalog_mapping earns real weight
    # so a résumé whose entities resolve cleanly to platform ids scores higher than
    # one that parsed but barely mapped.
    total = (
        personal * 0.30 + experience * 0.30 + education * 0.15
        + skills * 0.10 + mapping * 0.15
    )
    return round(total, 2)
