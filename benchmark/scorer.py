"""Pure scoring logic for the resume-parser benchmark.

Compares a parser output payload (the ``data`` object of a /resume/parse response)
against a hand-labelled *expected* record and returns per-metric (hits, total)
counts plus a list of human-readable misses. No I/O, no PII, no network - so it is
unit-testable and safe to commit; the labelled dataset it runs on lives in the
gitignored ``benchmark/data/`` (candidate PII).

Metrics (each a recall-style hits/total, except where noted):

  contact          name / email / phone correct (normalised compare)
  credentials      post-nominals recalled
  role_recall      expected work-history roles found in the output
  role_precision   output roles that correspond to a real expected role
                   (catches hallucinated/duplicated roles)
  date_accuracy    matched roles whose start+end dates are correct
  city_resolution  roles whose city IS in the platform catalog that got a city_id
                   (THE metric embedded-city name-mining should move)
  geo_ids          matched roles with the correct state_id
  education         expected institutions found
  negatives        false-positive checks that must NOT appear (phantom phone, etc.)

`city_resolution` deliberately counts only roles the expected record marks
``catalog_city: true`` - a city known to exist in GigHealth's list - so a genuine
catalog gap (e.g. Opelousas) is not charged against the parser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Score:
    """Accumulates hits/total per metric plus the misses behind them."""

    metrics: dict[str, list[int]] = field(default_factory=dict)   # name -> [hits, total]
    misses: list[str] = field(default_factory=list)

    def add(self, metric: str, hit: bool, miss_detail: str | None = None) -> None:
        slot = self.metrics.setdefault(metric, [0, 0])
        slot[1] += 1
        if hit:
            slot[0] += 1
        elif miss_detail:
            self.misses.append(f"{metric}: {miss_detail}")

    def merge(self, other: Score) -> None:
        for name, (h, t) in other.metrics.items():
            slot = self.metrics.setdefault(name, [0, 0])
            slot[0] += h
            slot[1] += t
        self.misses.extend(other.misses)

    def rate(self, metric: str) -> float | None:
        h, t = self.metrics.get(metric, [0, 0])
        return (h / t) if t else None

    def overall(self) -> float | None:
        """Unweighted mean of every metric's rate (metrics with no samples skipped)."""
        rates = [self.rate(m) for m in self.metrics if self.metrics[m][1]]
        rates = [r for r in rates if r is not None]
        return sum(rates) / len(rates) if rates else None


def _digits(v: str | None) -> str:
    return re.sub(r"\D", "", v or "")


def _norm(v: str | None) -> str:
    return re.sub(r"\s+", " ", (v or "").strip().lower())


def _find_role(actual_roles: list[dict], company_key: str) -> dict | None:
    """Match an expected role to an output role by normalised substring.

    Checks the company first, then the role title - some employers are genuine
    placeholders ("Many", "Unknown" for a self-employed caregiver), so a distinctive
    key like "travel lpn"/"nanny" is matched against the role instead.
    """
    key = _norm(company_key)
    if not key:
        return None
    for r in actual_roles:
        if key in _norm(r.get("company")) or key in _norm(r.get("role")):
            return r
    return None


def score_resume(actual: dict, expected: dict) -> Score:
    """Score one parser payload against its expected label."""
    s = Score()
    pi = actual.get("personal_info") or {}
    exp_pi = expected.get("personal_info") or {}

    # -- Contact ---------------------------------------------------------------
    if "full_name" in exp_pi:
        s.add("contact", _norm(pi.get("full_name")) == _norm(exp_pi["full_name"]),
              f"name {pi.get('full_name')!r} != {exp_pi['full_name']!r}")
    if "email" in exp_pi:
        s.add("contact", _norm(pi.get("email")) == _norm(exp_pi["email"]),
              f"email {pi.get('email')!r} != {exp_pi['email']!r}")
    if "phone_digits" in exp_pi:
        s.add("contact", _digits(pi.get("phone")) == exp_pi["phone_digits"],
              f"phone {pi.get('phone')!r} != digits {exp_pi['phone_digits']}")

    for cred in exp_pi.get("credentials", []):
        have = {_norm(c) for c in pi.get("credentials", [])}
        s.add("credentials", _norm(cred) in have, f"missing credential {cred!r}")

    # -- Roles: recall, dates, city resolution, geo ids ------------------------
    actual_roles = actual.get("experience") or []
    exp_roles = expected.get("experience") or []
    matched_actual: list[int] = []
    for er in exp_roles:
        ar = _find_role(actual_roles, er["company_key"])
        s.add("role_recall", ar is not None, f"role {er['company_key']!r} not found")
        if ar is None:
            continue
        matched_actual.append(id(ar))

        if er.get("start_date") is not None:
            s.add("date_accuracy", _norm(ar.get("start_date")) == _norm(er["start_date"]),
                  f"{er['company_key']}: start {ar.get('start_date')!r} != {er['start_date']!r}")
        if er.get("end_date") is not None:
            s.add("date_accuracy", _norm(ar.get("end_date")) == _norm(er["end_date"]),
                  f"{er['company_key']}: end {ar.get('end_date')!r} != {er['end_date']!r}")

        if er.get("catalog_city"):
            s.add("city_resolution", ar.get("city_id") is not None,
                  f"{er['company_key']}: city {er.get('city')!r} -> city_id null")
        if er.get("state_id") is not None:
            s.add("geo_ids", str(ar.get("state_id")) == str(er["state_id"]),
                  f"{er['company_key']}: state_id {ar.get('state_id')} != {er['state_id']}")

    # -- Role precision: every output role should be a real one ----------------
    for ar in actual_roles:
        hay = _norm(ar.get("company")) + " || " + _norm(ar.get("role"))
        is_expected = any(_norm(er["company_key"]) in hay for er in exp_roles)
        s.add("role_precision", is_expected, f"unexpected role {ar.get('company')!r}")

    # -- Education -------------------------------------------------------------
    edu_names = [_norm(e.get("institution")) for e in actual.get("education") or []]
    for inst in expected.get("education_keys", []):
        s.add("education", any(_norm(inst) in name for name in edu_names),
              f"institution {inst!r} not found")

    # -- Negatives (false positives that must NOT appear) ----------------------
    neg = expected.get("negatives") or {}
    for bad in neg.get("no_phone_secondary_digits", []):
        s.add("negatives", _digits(pi.get("phone_secondary")) != bad,
              f"phantom phone_secondary {pi.get('phone_secondary')!r}")
    for bad_id in neg.get("no_specialty_id", []):
        found = any(
            str(sp.get("specialty_id")) == str(bad_id)
            for r in actual_roles for sp in (r.get("specialties") or [])
        )
        s.add("negatives", not found, f"spurious specialty_id {bad_id}")

    return s
