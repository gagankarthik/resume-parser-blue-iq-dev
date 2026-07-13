"""Shared field sanitizers used across the schema models.

These SANITIZE (return None/default) rather than raise, so malformed LLM output
never crashes the pipeline.
"""

from __future__ import annotations

import re
from datetime import date as _date

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_URL_RE   = re.compile(r"^https?://", re.I)

# Month-name -> number, accepting 3-letter prefixes (jan, sept, etc.).
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _sanitize_email(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip().lower()
    return v if _EMAIL_RE.match(v) else None


def _sanitize_url(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip()
    if not v:
        return None
    if not _URL_RE.match(v):
        # Only promote a scheme-less value to https:// when it actually looks like
        # a host (has a dot, no whitespace). Otherwise junk the model emits for an
        # absent link - "N/A", "not provided", "available upon request" - would
        # become a clickable, broken "https://N/A" link. Better to drop it.
        if " " in v or "." not in v:
            return None
        v = f"https://{v}"
    return v if len(v) <= 2048 else None


def _fmt_mdy(mo: int, day: int, year: int) -> str | None:
    # Reject impossible calendar dates (e.g. 02/30, 04/31) instead of echoing them.
    if not 1900 <= year <= 2035:
        return None
    try:
        _date(year, mo, day)
    except ValueError:
        return None
    return f"{mo:02d}/{day:02d}/{year}"


def _fmt_my(mo: int, year: int) -> str | None:
    if not (1 <= mo <= 12 and 1900 <= year <= 2035):
        return None
    return f"{mo:02d}/{year}"


def _expand_yy(yy: int) -> int:
    """Expand a 2-digit year using a 1969-2068 pivot (e.g. 19 -> 2019, 98 -> 1998)."""
    return 2000 + yy if yy < 69 else 1900 + yy


def _sanitize_date(v: str | None) -> str | None:
    """Normalize a date to MM/DD/YYYY, MM/YYYY, or YYYY - preserving the precision
    actually stated on the resume. 'Present' passes through; unparseable -> None.

    Crucially, this NEVER invents a missing day or month. A month/year value such
    as 'August 2018' becomes '08/2018' (not '08/01/2018'), and a year-only value
    stays 'YYYY'. Accepts ISO (YYYY-MM-DD / YYYY-MM), US numeric (M/D/YYYY,
    M/YYYY), and written forms ('Aug 2018', 'July 21, 2019', 'July 21st 2019').
    """
    if not v:
        return None
    v = str(v).strip()
    if not v:
        return None
    if v.lower() == "present":
        return "Present"

    # ISO full / year-month
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", v)
    if m:
        return _fmt_mdy(int(m[2]), int(m[3]), int(m[1]))
    m = re.match(r"^(\d{4})-(\d{1,2})$", v)
    if m:
        return _fmt_my(int(m[2]), int(m[1]))

    # US numeric full / month-year  (slash or hyphen separated)
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", v)
    if m:
        return _fmt_mdy(int(m[1]), int(m[2]), int(m[3]))
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2})$", v)
    if m:
        return _fmt_mdy(int(m[1]), int(m[2]), _expand_yy(int(m[3])))
    m = re.match(r"^(\d{1,2})[/-](\d{4})$", v)
    if m:
        return _fmt_my(int(m[1]), int(m[2]))
    # Numeric month + 2-digit year ("4/19" -> 04/2019, "12/25" -> 12/2025). On a
    # resume this form is a month/year ("August 2018 - April 19"), not a
    # day-of-month without a year. A modest future window keeps cert expiry
    # dates ("exp 4/27") parseable.
    m = re.match(r"^(\d{1,2})[/-](\d{2})$", v)
    if m and 1 <= int(m[1]) <= 12:
        year = _expand_yy(int(m[2]))
        if year <= _date.today().year + 10:
            return _fmt_my(int(m[1]), year)

    # Written: "Month DD, YYYY" / "Month DDth YYYY"
    m = re.match(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$", v)
    if m and m[1][:3].lower() in _MONTHS:
        return _fmt_mdy(_MONTHS[m[1][:3].lower()], int(m[2]), int(m[3]))

    # Written: "Month YYYY"
    m = re.match(r"^([A-Za-z]{3,9})\.?\s+(\d{4})$", v)
    if m and m[1][:3].lower() in _MONTHS:
        return _fmt_my(_MONTHS[m[1][:3].lower()], int(m[2]))

    # Written with a 2-digit year: "Month 'YY" (apostrophe = explicit year),
    # "Month YY" where YY cannot be a day-of-month (>31), or "Month YY" where the
    # expanded year is in the past ("April 19" -> 04/2019 - resume date ranges end
    # in years, and a bare day-of-month with no year is useless anyway). Only a
    # value that would expand to a FUTURE year ("June 30" -> 2030?) stays ambiguous,
    # so we do not guess - better None than a wrong date.
    m = re.match(r"^([A-Za-z]{3,9})\.?\s+('?)(\d{2})$", v)
    if m and m[1][:3].lower() in _MONTHS:
        yy = int(m[3])
        year = _expand_yy(yy)
        if m[2] == "'" or yy > 31 or year <= _date.today().year:
            return _fmt_my(_MONTHS[m[1][:3].lower()], year)

    # Year only
    m = re.match(r"^(\d{4})$", v)
    if m:
        year = int(m[1])
        return str(year) if 1900 <= year <= 2035 else None

    return None


def _sanitize_phone(v: str | None) -> str | None:
    """Keep phones with a plausible digit count (7-15), preserving format.

    Counts TOTAL digits, not consecutive ones - a formatted number like
    '(555) 234-5678' has no run of 6+ digits yet is perfectly valid.
    """
    if not v:
        return None
    v = v.strip()
    digit_count = len(re.sub(r"\D", "", v))
    return v if 7 <= digit_count <= 15 else None


def _sanitize_year(v: int | None) -> int | None:
    if v is None:
        return None
    return v if 1900 <= v <= 2035 else None


def _sanitize_str(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip()
    return v if v else None


def _sanitize_yes_no_na(v: str | None) -> str | None:
    """Coerce a tri-state facility flag to exactly 'Yes', 'No', or 'N/A'.

    Anything that doesn't clearly read as yes/no/na (including blanks) becomes
    None so an unstated flag is never invented.
    """
    if not v:
        return None
    key = v.strip().lower().replace(".", "").replace("/", "")
    if key in {"yes", "y", "true"}:
        return "Yes"
    if key in {"no", "n", "false"}:
        return "No"
    if key in {"na", "n a", "notapplicable", "none"}:
        return "N/A"
    return None


def _coerce_list(v) -> list:
    """Ensure list fields are always lists.

    Guards against two common LLM slips: returning null (-> []) and returning a
    lone object where an array of objects was expected (-> wrap it, so the whole
    section isn't silently dropped). Scalars/other types -> []. String-list fields
    filter out any wrapped object downstream (see coerce_string_lists).
    """
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v]
    return []
