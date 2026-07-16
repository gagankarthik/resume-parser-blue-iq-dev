"""Unit tests for the benchmark scorer - synthetic data only, no PII, no I/O."""

from benchmark.scorer import score_resume


def _actual(**over):
    base = {
        "personal_info": {"full_name": "Test User", "email": "t@x.com",
                          "phone": "(555) 234-5678", "credentials": ["RN"],
                          "phone_secondary": None},
        "experience": [
            {"company": "United Memorial Medical Center", "role": "RN",
             "start_date": "08/2021", "end_date": "Present",
             "city": "Batavia", "city_id": "999", "state_id": "35", "specialties": []},
        ],
        "education": [{"institution": "Niagara University"}],
    }
    base.update(over)
    return base


def _expected(**over):
    base = {
        "personal_info": {"full_name": "Test User", "email": "t@x.com",
                          "phone_digits": "5552345678", "credentials": ["RN"]},
        "experience": [
            {"company_key": "united memorial", "start_date": "08/2021", "end_date": "Present",
             "city": "Batavia", "catalog_city": True, "state_id": "35"},
        ],
        "education_keys": ["niagara university"],
    }
    base.update(over)
    return base


def test_perfect_match_scores_100():
    s = score_resume(_actual(), _expected())
    assert s.overall() == 1.0
    assert s.rate("contact") == 1.0
    assert s.rate("city_resolution") == 1.0


def test_phone_compared_by_digits_ignoring_format():
    a = _actual()
    a["personal_info"]["phone"] = "555.234.5678"      # different punctuation, same number
    assert score_resume(a, _expected()).rate("contact") == 1.0


def test_missing_role_counts_against_recall():
    a = _actual(experience=[])
    s = score_resume(a, _expected())
    assert s.rate("role_recall") == 0.0


def test_role_matched_by_role_title_when_company_is_placeholder():
    a = _actual(experience=[{"company": "Many", "role": "Travel LPN",
                             "city": None, "city_id": None, "state_id": None, "specialties": []}])
    e = _expected(experience=[{"company_key": "travel lpn"}])
    assert score_resume(a, e).rate("role_recall") == 1.0


def test_city_null_fails_resolution_only_when_catalog_city():
    a = _actual()
    a["experience"][0]["city_id"] = None
    # catalog_city True -> charged
    assert score_resume(a, _expected()).rate("city_resolution") == 0.0
    # catalog_city False (a genuine catalog gap) -> not scored at all
    e = _expected(experience=[{"company_key": "united memorial", "city": "Opelousas",
                               "catalog_city": False, "state_id": "19"}])
    assert score_resume(a, e).rate("city_resolution") is None


def test_phantom_phone_secondary_fails_negatives():
    a = _actual()
    a["personal_info"]["phone_secondary"] = "950-1200"
    e = _expected(negatives={"no_phone_secondary_digits": ["9501200"]})
    assert score_resume(a, e).rate("negatives") == 0.0
    # absent -> passes
    a["personal_info"]["phone_secondary"] = None
    assert score_resume(a, e).rate("negatives") == 1.0


def test_hallucinated_role_hurts_precision_not_recall():
    a = _actual()
    a["experience"].append({"company": "Ghost Corp", "role": "Wizard",
                            "city": None, "city_id": None, "state_id": None, "specialties": []})
    s = score_resume(a, _expected())
    assert s.rate("role_recall") == 1.0        # the real one is still found
    assert s.rate("role_precision") == 0.5     # 1 of 2 output roles is real


def test_spurious_specialty_id_fails_negatives():
    a = _actual()
    a["experience"][0]["specialties"] = [{"name": "Director", "specialty_id": "737"}]
    e = _expected(negatives={"no_specialty_id": ["737"]})
    assert score_resume(a, e).rate("negatives") == 0.0
