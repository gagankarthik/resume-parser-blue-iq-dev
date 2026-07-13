"""
Facility matcher tests - exact name -> id at conf 1.0, conservative fuzzy for a
near-miss spelling, and a graceful miss (id null, never guessed) when unmatched or
no catalog is loaded.
"""

import json

import pytest

from app.services.normalization import facility_catalog, facility_matcher


@pytest.fixture(autouse=True)
def _catalog(tmp_path):
    path = tmp_path / "fac.json"
    path.write_text(json.dumps({"facilities": [
        {"id": "3022", "name": "60th Medical Group - Travis AFB",
         "health_system": "Defense Health Agency", "health_system_id": "181"},
        {"id": "2974", "name": "Fort Sanders Regional Medical Center"},
    ]}), encoding="utf-8")
    facility_catalog.reload(str(path))
    yield
    facility_catalog.reload("")


def test_exact_name_matches_at_full_confidence():
    m = facility_matcher.match("Fort Sanders Regional Medical Center")
    assert m.matched
    assert m.facility_id == "2974"
    assert m.confidence == 1.0
    assert m.match_tier == "name"


def test_punctuation_and_case_insensitive():
    m = facility_matcher.match("fort sanders regional medical center")
    assert m.facility_id == "2974"
    assert m.confidence == 1.0


def test_parenthetical_and_dash_normalised_to_exact():
    m = facility_matcher.match("60th Medical Group – Travis AFB (Main Campus)")
    assert m.facility_id == "3022"
    assert m.confidence == 1.0
    assert m.health_system == "Defense Health Agency"


def test_near_miss_typo_fuzzy_matches_below_exact():
    m = facility_matcher.match("Fort Sanders Regionl Medical Center")  # dropped 'a'
    assert m.matched
    assert m.facility_id == "2974"
    assert m.match_tier == "fuzzy"
    assert 0.90 <= m.confidence <= facility_matcher.CONF_FUZZY_MAX


def test_unrelated_name_is_unmatched():
    m = facility_matcher.match("Acme Widgets LLC")
    assert not m.matched
    assert m.facility_id is None
    assert m.confidence == 0.0


@pytest.mark.parametrize("placeholder", ["", "Unknown", "N/A", "  "])
def test_placeholders_skip(placeholder):
    m = facility_matcher.match(placeholder)
    assert not m.matched
    assert m.facility_id is None


def test_no_catalog_is_graceful_miss():
    facility_catalog.reload("")
    m = facility_matcher.match("Fort Sanders Regional Medical Center")
    assert not m.matched
    assert m.facility_id is None


# -- Containment tier ----------------------------------------------------------
#
# Resumes drop the legal prefix a catalog carries. The parse that surfaced this had
# "Oishei Children's Hospital"; the real catalog calls it "John R Oishei Childrens
# Hospital". Whole-string fuzzy scores that pair at 0.893 - under the 0.90 floor by
# seven thousandths - and lowering the floor would start admitting look-alikes.
#
# These use their own stub catalog that reproduces the SHAPE of the real one (a
# dropped legal prefix, an ambiguous surname, a generic-only name). Coupling them to
# the shipped 1.4 MB snapshot would make them break on every catalog re-sync.
#
# The shipped catalog was validated separately across all 8217 records: containment
# recovered 1775 dropped-prefix names and picked a WRONG record 0 times.


@pytest.fixture
def _rich_catalog(tmp_path):
    path = tmp_path / "fac_rich.json"
    path.write_text(json.dumps({"facilities": [
        # The bug: resume says "Oishei Children's Hospital", catalog carries a prefix.
        {"id": "4308", "name": "John R Oishei Childrens Hospital"},
        # Ambiguity: "Riverside" alone fits BOTH of these.
        {"id": "5685", "name": "Riverside Regional Medical Center",
         "health_system": "Riverside Health System", "health_system_id": "476"},
        {"id": "9001", "name": "Riverside Community Hospital"},
        # A health SYSTEM, not the cardiology office a resume might name.
        {"id": "234",  "name": "Great Lakes Health System of Western New York"},
    ]}), encoding="utf-8")
    facility_catalog.reload(str(path))
    yield
    facility_catalog.reload("")


def test_dropped_legal_prefix_resolves_via_containment(_rich_catalog):
    """THE REGRESSION. The facility IS in the catalog; we were failing to find it."""
    for spelling in ["Oishei Children's Hospital", "Oishei Children’s Hospital"]:
        res = facility_matcher.match(spelling)
        assert res.facility_id == "4308", f"{spelling!r} should resolve to John R Oishei"
        assert res.match_tier == "containment"
        assert res.confidence == facility_matcher.CONF_CONTAINMENT


def test_containment_refuses_a_purely_generic_name(_rich_catalog):
    """'Regional' / 'Medical' / 'Center' describe half the catalog. A shorthand made
    only of generic tokens must never resolve by containment - it identifies nothing."""
    for junk in ["Community Health Center", "Medical Center", "Hospital", "Children's Hospital"]:
        res = facility_matcher.match(junk)
        assert res.match_tier != "containment", f"{junk!r} must not containment-match"


def test_containment_refuses_an_ambiguous_shorthand(_rich_catalog):
    """'Riverside' is a subset of BOTH Riverside records. Two facilities fit, so the
    matcher must refuse to choose rather than stamp a coin-flip id."""
    res = facility_matcher.match("Riverside")
    assert res.facility_id is None and not res.matched


def test_containment_never_preempts_an_exact_match(_rich_catalog):
    """Tier order matters: an exact name must still win at confidence 1.0, even though
    its tokens are also a subset of nothing else."""
    res = facility_matcher.match("Riverside Regional Medical Center")
    assert res.facility_id == "5685"
    assert res.match_tier == "name" and res.confidence == 1.0


def test_facility_genuinely_absent_stays_null(_rich_catalog):
    """Great Lakes Cardiovascular is a cardiology office, not a catalog facility. The
    only 'Great Lakes' record is a health SYSTEM - matching it would be wrong."""
    res = facility_matcher.match("Great Lakes Cardiovascular")
    assert res.facility_id is None and not res.matched
