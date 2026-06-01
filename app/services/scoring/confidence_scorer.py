"""
Per-field confidence scoring.

Scores reflect how complete and verifiable each section is.
Used by enterprise clients to flag records that need human review.

Score range: 0.0 – 1.0
"""

import re
from app.models.schemas import ParsedResumeAI, ConfidenceScores

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"[\d]{7,}")


def score(parsed: ParsedResumeAI) -> ConfidenceScores:
    return ConfidenceScores(
        personal_info=_score_personal(parsed),
        experience=_score_experience(parsed),
        education=_score_education(parsed),
        skills=_score_skills(parsed),
        overall=_overall(parsed),
    )


def _score_personal(parsed: ParsedResumeAI) -> float:
    p = parsed.personal_info
    points = 0.0
    total = 5.0

    if p.full_name and len(p.full_name.split()) >= 2:
        points += 1.0
    if p.email and _EMAIL_RE.match(p.email):
        points += 1.5
    if p.phone and _PHONE_RE.search(p.phone):
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


def _overall(parsed: ParsedResumeAI) -> float:
    weights = {
        "personal": (_score_personal(parsed), 0.35),
        "experience": (_score_experience(parsed), 0.35),
        "education": (_score_education(parsed), 0.20),
        "skills": (_score_skills(parsed), 0.10),
    }
    total = sum(s * w for s, w in weights.values())
    return round(total, 2)
