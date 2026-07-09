"""
Deterministic, rule-based resume parser — a no-LLM "floor".

We borrow only the IDEA behind open-resume (github.com/xitanggg/open-resume) —
"always keep a deterministic parser that can't time out or hallucinate" — not its
implementation. open-resume relies on clean, well-formatted single-column PDFs and
their positional/font metadata; our résumés come from the medical (non-technical)
side and are rarely formatted consistently, so a layout-driven port would fail on
them. This parser is therefore OUR OWN: it works on the already-extracted plain
text and is tuned for how nursing / allied-health résumés actually read — travel
and per-diem role blocks, healthcare degree keywords (ADN/BSN/MSN…), and the
Month-YYYY / MM-YYYY date ranges these résumés use.

It has no semantic understanding and is weaker than the LLM on messy prose, but it
NEVER times out and NEVER invents data — the ideal fallback when the AI parse is
cut off, so a degraded parse still carries real experience, education, and skills
instead of contact details only. Everything is best-effort and conservative: a
value that can't be read with reasonable confidence is left null, never guessed.
The output is flagged `partial` upstream and is meant for human review. Improve
the heuristics here (not by adopting open-resume) as new résumé shapes surface.
"""

import re

from app.models.schemas import (
    CertificationItem,
    EducationItem,
    ExperienceItem,
    ParsedResumeAI,
    PersonalInfo,
)
from app.services.parsing import section_detector
from app.services.parsing.rule_parser import RuleExtracted

# ── Date parsing ──────────────────────────────────────────────────────────────
_MONTHS = (
    "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    "aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
# A single date token: "November 2024", "Nov 2024", "November, 2024" (a comma
# between month and year is common — "April, 2025"), "08/2024", "8/2024", "2024".
_DATE_TOKEN = rf"(?:(?:{_MONTHS})\.?,?\s+\d{{4}}|\d{{1,2}}/\d{{4}}|\d{{4}})"
# A range: "<date> - <date|Present>" with various dashes/"to" separators.
_DATE_RANGE = re.compile(
    rf"(?P<start>{_DATE_TOKEN})\s*(?:[-–—]|to)\s*(?P<end>{_DATE_TOKEN}|present|current)",
    re.IGNORECASE,
)

_MONTH_NUM = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}

_DEGREE_KEYWORDS = re.compile(
    r"\b(associate(?:'?s)?|bachelor(?:'?s)?|master(?:'?s)?|doctor(?:ate)?|ph\.?d|"
    r"diploma|certificate|a\.?d\.?n|b\.?s\.?n|m\.?s\.?n|b\.?s\.?|m\.?s\.?|b\.?a\.?|"
    r"m\.?a\.?|b\.?s\.?n\.?|d\.?n\.?p|m\.?b\.?a)\b",
    re.IGNORECASE,
)
_INSTITUTION_KEYWORDS = re.compile(
    r"\b(university|college|school|institute|academy|seminary|polytechnic)\b",
    re.IGNORECASE,
)
_YEAR = re.compile(r"\b(19|20)\d{2}\b")

# A line that is almost certainly a bullet/duty, not a heading.
_BULLET = re.compile(r"^\s*(?:[•\-\*▪–•o]|\d+[.)])\s+")


def _norm_date(token: str) -> str | None:
    """Normalize a raw date token to MM/YYYY or YYYY (never inventing a day)."""
    token = token.strip()
    if token.lower() in ("present", "current"):
        return "Present"
    m = re.match(rf"({_MONTHS})\.?,?\s+(\d{{4}})", token, re.IGNORECASE)
    if m:
        return f"{_MONTH_NUM[m.group(1)[:3].lower()]}/{m.group(2)}"
    m = re.match(r"(\d{1,2})/(\d{4})", token)
    if m:
        return f"{int(m.group(1)):02d}/{m.group(2)}"
    m = re.match(r"(\d{4})$", token)
    if m:
        return m.group(1)
    return None


# ── Name ──────────────────────────────────────────────────────────────────────
_CREDENTIAL_TAIL = re.compile(r"\s*,\s*[A-Za-z().\-/ ]+$")


def _looks_like_name(line: str) -> bool:
    s = line.strip()
    if not s or "@" in s or any(c.isdigit() for c in s):
        return False
    if len(s) > 60:
        return False
    words = s.split()
    if not (1 <= len(words) <= 6):
        return False
    # Mostly letters, spaces, and a few name punctuation marks.
    letters = sum(c.isalpha() or c in " .,'-" for c in s)
    return letters / len(s) > 0.85


def _extract_name(header_text: str) -> str | None:
    for line in header_text.splitlines():
        if _looks_like_name(line):
            # Drop a trailing credential run ("Jane Smith, RN BSN" → "Jane Smith").
            name = line.strip()
            # Only strip after a comma so we don't clip a real surname.
            if "," in name:
                name = _CREDENTIAL_TAIL.sub("", name).strip() or name
            return name
    return None


# ── Experience ────────────────────────────────────────────────────────────────
def _extract_experience(text: str) -> list[ExperienceItem]:
    """Split an experience section into entries anchored on date ranges.

    Each date-range line starts (or continues) an entry; the 1–2 non-bullet lines
    immediately above supply role/company, and the lines below (until the next
    date range) become description bullets. Conservative: emits an entry only when
    a date range is present, so prose without dates is never fabricated into jobs.
    """
    # PDFs often wrap a date range across a line break ("April, 2025 -\npresent",
    # "December, 2023 - October,\n2025"). The anchoring below is line-based, so
    # collapse any newline INSIDE a matched range onto one line first — otherwise
    # every such role is invisible and the record comes back with zero experience.
    text = _DATE_RANGE.sub(lambda m: m.group(0).replace("\n", " "), text)
    lines = [ln.rstrip() for ln in text.splitlines()]
    entries: list[ExperienceItem] = []

    # Index every line that carries a date range.
    anchors = [i for i, ln in enumerate(lines) if _DATE_RANGE.search(ln)]
    if not anchors:
        return []

    for pos, idx in enumerate(anchors):
        line = lines[idx]
        m = _DATE_RANGE.search(line)
        start = _norm_date(m.group("start")) if m else None
        end = _norm_date(m.group("end")) if m else None

        # Heading lines: the non-bullet, non-empty lines just above this anchor,
        # bounded by the previous anchor's block.
        prev_anchor = anchors[pos - 1] if pos > 0 else -1
        heading: list[str] = []
        j = idx - 1
        while j > prev_anchor and len(heading) < 2:
            cand = lines[j].strip()
            if cand and not _BULLET.match(lines[j]) and not _DATE_RANGE.search(cand):
                heading.insert(0, cand)
            elif not cand:
                if heading:
                    break
            j -= 1

        # Also allow role/company text on the date line itself (date stripped out).
        on_line = _DATE_RANGE.sub("", line).strip(" ,-–—|\t")
        if on_line and on_line not in heading:
            heading.append(on_line)

        role = heading[0] if heading else None
        company = heading[1] if len(heading) > 1 else None

        # Description: bullets/lines below this anchor until the next anchor —
        # but the 1–2 non-bullet lines immediately above the NEXT anchor are that
        # entry's heading (role/company), not this entry's description. Find where
        # the next heading starts and stop there (mirrors the heading look-back).
        next_anchor = anchors[pos + 1] if pos + 1 < len(anchors) else len(lines)
        desc_end = next_anchor
        if pos + 1 < len(anchors):
            taken = 0
            j2 = next_anchor - 1
            while j2 > idx and taken < 2:
                cand2 = lines[j2].strip()
                if cand2 and not _BULLET.match(lines[j2]) and not _DATE_RANGE.search(cand2):
                    desc_end = j2
                    taken += 1
                elif not cand2:
                    if taken:
                        break
                j2 -= 1
        desc: list[str] = []
        for k in range(idx + 1, desc_end):
            cand = lines[k].strip()
            if not cand:
                continue
            desc.append(re.sub(r"^\s*[•\-\*▪–•o]\s+", "", cand))

        entries.append(
            ExperienceItem(
                role=role,
                company=company,
                start_date=start,
                end_date=end,
                is_current=(end == "Present"),
                description=[d for d in desc if d][:40],
                achievements=[],
                specialties=[],
            )
        )
        if len(entries) >= 60:
            break
    return entries


# ── Education ─────────────────────────────────────────────────────────────────
def _extract_education(text: str) -> list[EducationItem]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    items: list[EducationItem] = []
    for i, line in enumerate(lines):
        has_degree = _DEGREE_KEYWORDS.search(line)
        has_inst = _INSTITUTION_KEYWORDS.search(line)
        if not (has_degree or has_inst):
            continue
        degree = line if has_degree else None
        institution = line if has_inst and not has_degree else None
        # Look at the adjacent line for the missing half.
        neighbours = []
        if i + 1 < len(lines):
            neighbours.append(lines[i + 1])
        if i > 0:
            neighbours.append(lines[i - 1])
        for nb in neighbours:
            if institution is None and _INSTITUTION_KEYWORDS.search(nb) and not _DEGREE_KEYWORDS.search(nb):
                institution = nb
            if degree is None and _DEGREE_KEYWORDS.search(nb):
                degree = nb
        # A graduation year often sits on its own line just below the degree /
        # institution — scan the entry line and the next two.
        grad_year = None
        for cand in (line, *lines[i + 1 : i + 3]):
            ym = _YEAR.search(cand)
            if ym:
                grad_year = int(ym.group(0))
                break
        # Avoid duplicate entries for the same degree line.
        if any(e.degree == degree and e.institution == institution for e in items):
            continue
        items.append(
            EducationItem(
                institution=(institution or None),
                degree=(degree or None),
                field_of_study=None,
                start_year=None,
                graduation_year=grad_year,
                gpa=None,
            )
        )
        if len(items) >= 20:
            break
    return items


# ── Skills / certifications ───────────────────────────────────────────────────
_SPLIT = re.compile(r"[••▪|,;/]|\s{2,}|(?:^|\s)[-\*]\s")


def _split_items(text: str, cap: int) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        for tok in _SPLIT.split(line):
            t = (tok or "").strip(" \t-•*·")
            if 2 <= len(t) <= 80 and not t.isdigit():
                out.append(t)
    # De-dupe, preserve order.
    seen: set[str] = set()
    uniq = []
    for t in out:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            uniq.append(t)
    return uniq[:cap]


def _extract_certifications(text: str) -> list[CertificationItem]:
    names = _split_items(text, cap=40)
    return [CertificationItem(name=n) for n in names]


# ── Entry point ───────────────────────────────────────────────────────────────
def parse(text: str, anchors: RuleExtracted) -> ParsedResumeAI:
    """Best-effort deterministic parse. Never raises, never invents data."""
    sections = section_detector.detect(text)

    header = sections.get("header", "")
    if not header and "full_text" in sections:
        header = "\n".join(sections["full_text"].splitlines()[:5])

    personal = PersonalInfo(
        full_name=_extract_name(header),
        email=anchors.emails[0] if anchors.emails else None,
        phone=anchors.phones[0] if anchors.phones else None,
        linkedin_url=anchors.linkedin_urls[0] if anchors.linkedin_urls else None,
        github_url=anchors.github_urls[0] if anchors.github_urls else None,
        portfolio_url=anchors.portfolio_urls[0] if anchors.portfolio_urls else None,
        summary=(sections.get("summary") or None),
    )

    # Prefer segmented sections; fall back to scanning the whole text for
    # experience so a résumé with no detectable headers still yields entries.
    exp_src = sections.get("experience") or sections.get("full_text") or ""
    experience = _extract_experience(exp_src)

    education = _extract_education(sections.get("education", ""))
    skills = _split_items(sections.get("skills", ""), cap=60)
    certifications = _extract_certifications(sections.get("certifications", ""))

    return ParsedResumeAI(
        personal_info=personal,
        experience=experience,
        education=education,
        skills=skills,
        certifications=certifications,
    )
