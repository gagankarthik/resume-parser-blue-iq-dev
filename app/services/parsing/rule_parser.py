"""
Regex-based extraction for high-confidence fields.
These run before the AI parser so the model receives pre-extracted anchors.
"""

import re
from dataclasses import dataclass, field

# A bare year range ("2015 - 2019") has 8 digits and otherwise looks phone-shaped,
# so it must be rejected before it lands in `phones` and is handed to the model as
# an authoritative contact number.
_YEAR_RANGE = re.compile(
    r"^\s*(?:19|20)\d{2}\s*[-–—]\s*(?:(?:19|20)\d{2}|present)\s*$", re.I
)

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
# OCR fallback: Tesseract often injects a space next to the @ when reading
# underlined hyperlink text ("Katherine.Driscoll@ Baycare.org"). Only consulted
# when the strict pattern finds nothing; matches are normalized by removing the
# spaces.
_EMAIL_LOOSE = re.compile(r"\b[A-Za-z0-9._%+\-]+ ?@ ?[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(
    r"(?<!\d)(\+?[\d][\d\s\-\.\(\)]{6,18}[\d])(?!\d)"
)
_LINKEDIN = re.compile(
    r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w\-]+/?", re.I
)
_GITHUB = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/[\w\-]+/?", re.I
)
_PORTFOLIO = re.compile(
    r"https?://(?!(?:www\.)?(?:linkedin|github)\.com)[\w\-\.]+\.[a-z]{2,}(?:/[\w\-\._~:/?#\[\]@!$&'()*+,;=%]*)?",
    re.I,
)


@dataclass
class RuleExtracted:
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    linkedin_urls: list[str] = field(default_factory=list)
    github_urls: list[str] = field(default_factory=list)
    portfolio_urls: list[str] = field(default_factory=list)


def extract(text: str) -> RuleExtracted:
    result = RuleExtracted()

    result.emails = list(dict.fromkeys(_EMAIL.findall(text)))
    if not result.emails:
        result.emails = list(dict.fromkeys(
            m.group().replace(" ", "") for m in _EMAIL_LOOSE.finditer(text)
        ))

    raw_phones = _PHONE.findall(text)
    # Normalize: strip non-digit chars, keep only plausible lengths, and drop two
    # classes of phone-shaped noise the digit gate would otherwise hand the model as
    # an authoritative contact number:
    #   * a bare year range ("2015 - 2019") — 8 digits — via _YEAR_RANGE;
    #   * a short 7-9 digit token with no country code, e.g. an equipment model/part
    #     range ("LTV 950-1200" -> "950-1200") mined from a skills line. A real contact
    #     number carries an area code (>=10 digits) or an explicit international "+".
    #     This is deliberately US-centric (the product's resume corpus): a bare
    #     sub-10-digit local number is uncallable and, in practice, always noise.
    seen: set[str] = set()
    for p in raw_phones:
        if _YEAR_RANGE.match(p):
            continue
        digits = re.sub(r"\D", "", p)
        has_country_code = p.lstrip().startswith("+")
        if not (10 <= len(digits) <= 15 or (has_country_code and 7 <= len(digits) <= 15)):
            continue
        if digits not in seen:
            seen.add(digits)
            result.phones.append(p.strip())

    result.linkedin_urls = list(dict.fromkeys(m.group() for m in _LINKEDIN.finditer(text)))
    result.github_urls = list(dict.fromkeys(m.group() for m in _GITHUB.finditer(text)))

    # Portfolio: exclude linkedin/github matches
    portfolio_candidates = _PORTFOLIO.findall(text)
    result.portfolio_urls = [
        u for u in dict.fromkeys(portfolio_candidates)
        if "linkedin.com" not in u and "github.com" not in u
    ]

    return result
