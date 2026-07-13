"""
Tests for healthcare taxonomy normalization.
Covers specialty abbreviation expansion, profession credentials, and role title expansion.
"""

from app.services.normalization.healthcare_taxonomy import expand_profession
from app.services.normalization.normalizer import _expand_role_credentials, _normalize_skills

# -- Specialty abbreviation expansion ------------------------------------------

def test_icu_expands():
    result = _normalize_skills(["ICU"])
    assert result == ["Intensive Care Unit"]

def test_nicu_expands():
    result = _normalize_skills(["NICU"])
    assert result == ["Neonatal Intensive Care Unit"]

def test_picu_expands():
    result = _normalize_skills(["PICU"])
    assert result == ["Pediatric Intensive Care Unit"]

def test_pacu_expands():
    result = _normalize_skills(["PACU"])
    assert result == ["Post-Anesthesia Care Unit"]

def test_er_expands():
    result = _normalize_skills(["ER"])
    assert result == ["Emergency Room"]

def test_bicu_expands():
    result = _normalize_skills(["BICU"])
    assert result == ["Burn Intensive Care Unit"]

def test_bmt_expands():
    result = _normalize_skills(["BMT"])
    assert result == ["Bone Marrow Transplant"]

def test_cticu_expands():
    result = _normalize_skills(["CTICU"])
    assert result == ["Cardiothoracic Intensive Care Unit"]

def test_ccu_expands():
    result = _normalize_skills(["CCU"])
    assert result == ["Critical Care Unit"]

def test_cvicu_expands():
    result = _normalize_skills(["CVICU"])
    assert result == ["Cardiovascular Intensive Care Unit"]

def test_cvor_expands():
    result = _normalize_skills(["CVOR"])
    assert result == ["Cardiovascular Operating Room"]

def test_micu_expands():
    result = _normalize_skills(["MICU"])
    assert result == ["Medical Intensive Care Unit"]

def test_sicu_expands():
    result = _normalize_skills(["SICU"])
    assert result == ["Surgical Intensive Care Unit"]

def test_snf_expands():
    result = _normalize_skills(["SNF"])
    assert result == ["Skilled Nursing Facility"]

def test_ltac_expands():
    result = _normalize_skills(["LTAC"])
    assert result == ["Long-Term Acute Care"]

def test_ltc_expands():
    result = _normalize_skills(["LTC"])
    assert result == ["Long-Term Care"]

def test_pcu_expands():
    result = _normalize_skills(["PCU"])
    assert result == ["Progressive Care Unit"]

def test_tcu_expands():
    result = _normalize_skills(["TCU"])
    assert result == ["Transitional Care Unit"]

def test_dou_expands():
    result = _normalize_skills(["DOU"])
    assert result == ["Definitive Observation Unit"]

def test_imcu_expands():
    result = _normalize_skills(["IMCU"])
    assert result == ["Intermediate Care Unit"]

def test_med_surg_expands():
    result = _normalize_skills(["Med Surg"])
    assert result == ["Medical Surgical"]

def test_med_surg_hyphenated_expands():
    # "Med-Surg" (hyphen) must resolve the same as "Med Surg" / "Med/Surg".
    assert _normalize_skills(["Med-Surg"]) == ["Medical Surgical"]
    assert _normalize_skills(["med-surg"]) == ["Medical Surgical"]
    assert _normalize_skills(["Med/Surg"]) == ["Medical Surgical"]

def test_hyphen_and_space_are_equivalent():
    # Hyphen vs space must not change resolution for multi-word specialties.
    assert _normalize_skills(["Med - Surg"]) == ["Medical Surgical"]
    assert _normalize_skills(["Med-Surg", "Med Surg"]) == ["Medical Surgical"]

def test_ob_gyn_expands():
    result = _normalize_skills(["OB/GYN"])
    assert result == ["Obstetrics and Gynecology"]

def test_preop_expands():
    result = _normalize_skills(["PreOp"])
    assert result == ["Pre-Operative"]

def test_tele_expands():
    result = _normalize_skills(["Tele"])
    assert result == ["Telemetry"]

def test_ep_lab_expands():
    result = _normalize_skills(["EP Lab"])
    assert result == ["Electrophysiology Laboratory"]

def test_gi_lab_expands():
    result = _normalize_skills(["GI Lab"])
    assert result == ["Gastrointestinal Laboratory"]

def test_rnfa_expands():
    result = _normalize_skills(["RNFA"])
    assert result == ["Registered Nurse First Assistant"]


# -- Allied Health abbreviations -----------------------------------------------

def test_rrt_expands():
    result = _normalize_skills(["RRT"])
    assert result == ["Registered Respiratory Therapist"]

def test_crt_expands():
    result = _normalize_skills(["CRT"])
    assert result == ["Certified Respiratory Therapist"]

def test_ct_tech_expands():
    result = _normalize_skills(["CT Tech"])
    assert result == ["CT Technologist (Computed Tomography)"]

def test_mri_tech_expands():
    result = _normalize_skills(["MRI Tech"])
    assert result == ["MRI Technologist (Magnetic Resonance Imaging)"]

def test_ekg_tech_expands():
    result = _normalize_skills(["EKG Tech"])
    assert result == ["EKG Technician (Electrocardiography)"]

def test_eeg_tech_expands():
    result = _normalize_skills(["EEG Tech"])
    assert result == ["EEG Technician (Electroencephalography)"]

def test_spt_expands():
    result = _normalize_skills(["SPT"])
    assert result == ["Sterile Processing Technician"]

def test_cst_expands():
    result = _normalize_skills(["CST"])
    assert result == ["Certified Surgical Technologist"]

def test_lcsw_expands():
    result = _normalize_skills(["LCSW"])
    assert result == ["Licensed Clinical Social Worker"]

def test_msw_expands():
    result = _normalize_skills(["MSW"])
    assert result == ["Masters of Social Work"]


# -- Profession credential expansion ------------------------------------------

def test_rn_expands():
    assert expand_profession("RN") == "Registered Nurse"

def test_lpn_expands():
    assert expand_profession("LPN") == "Licensed Practical Nurse"

def test_cna_expands():
    assert expand_profession("CNA") == "Certified Nursing Assistant"

def test_ot_expands():
    assert expand_profession("OT") == "Occupational Therapist"

def test_pt_expands():
    assert expand_profession("PT") == "Physical Therapist"

def test_slp_expands():
    assert expand_profession("SLP") == "Speech-Language Pathologist"

def test_pta_expands():
    assert expand_profession("PTA") == "Physical Therapist Assistant"

def test_cota_expands():
    assert expand_profession("COTA") == "Certified Occupational Therapy Assistant"


# -- Role title expansion ------------------------------------------------------

def test_role_rn_icu():
    assert _expand_role_credentials("RN - ICU") == "Registered Nurse - Intensive Care Unit"

def test_role_rn_nicu():
    result = _expand_role_credentials("RN - NICU")
    assert result == "Registered Nurse - Neonatal Intensive Care Unit"

def test_role_crt_nicu():
    result = _expand_role_credentials("CRT NICU")
    # CRT expands, NICU expands via suffix normalize
    assert "Certified Respiratory Therapist" in result

def test_role_unknown_unchanged():
    result = _expand_role_credentials("Staff Nurse")
    assert result == "Staff Nurse"


# -- Deduplication -------------------------------------------------------------

def test_dedup_same_specialty_different_case():
    result = _normalize_skills(["ICU", "icu", "Intensive Care Unit"])
    assert len(result) == 1
    assert result[0] == "Intensive Care Unit"

def test_dedup_abbreviation_and_full():
    result = _normalize_skills(["NICU", "Neonatal Intensive Care Unit"])
    assert len(result) == 1

def test_mixed_specialties_deduped():
    skills = ["RN", "ICU", "NICU", "PACU", "ER", "ICU"]
    result = _normalize_skills(skills)
    assert len(result) == len(set(s.lower() for s in result))


# -- Case insensitivity --------------------------------------------------------

def test_lowercase_icu():
    result = _normalize_skills(["icu"])
    assert result == ["Intensive Care Unit"]

def test_mixed_case_pacu():
    result = _normalize_skills(["Pacu"])
    assert result == ["Post-Anesthesia Care Unit"]

def test_uppercase_snf():
    result = _normalize_skills(["SNF"])
    assert result == ["Skilled Nursing Facility"]
