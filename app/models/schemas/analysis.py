"""Per-section confidence scores and skills-validation result models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ConfidenceScores(BaseModel):
    overall:         float = Field(..., ge=0.0, le=1.0, description="Weighted overall confidence (0.0 = no data, 1.0 = complete)")
    personal_info:   float = Field(..., ge=0.0, le=1.0, description="Confidence in personal contact fields")
    experience:      float = Field(..., ge=0.0, le=1.0, description="Confidence in experience entries")
    education:       float = Field(..., ge=0.0, le=1.0, description="Confidence in education entries")
    skills:          float = Field(..., ge=0.0, le=1.0, description="Confidence in skills / specialties list")
    catalog_mapping: float = Field(0.0, ge=0.0, le=1.0, description="Mean match confidence of the role entities resolved to platform ids (profession, facility, country, state, city, specialties). 1.0 = every entity matched a catalog id exactly; low = several fell back or went unmatched. 0.0 when there is nothing to map.")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "overall": 0.91,
                "personal_info": 0.96,
                "experience": 0.88,
                "education": 0.90,
                "skills": 1.0,
                "catalog_mapping": 0.94,
            }
        }
    )


# ── Skills validation ─────────────────────────────────────────────────────────

class SkillsValidation(BaseModel):
    """
    Validation of the parsed `skills` against the healthcare taxonomy.

    Each skill is classified as either *recognized* (it maps to a canonical
    specialty, profession/credential, or known clinical certification) or
    *unrecognized* (free-form, out-of-taxonomy). Use `recognized_ratio` to flag
    records whose skills could not be grounded and may need human review.
    """

    total:              int            = Field(..., ge=0, description="Total distinct skills validated")
    recognized_count:   int            = Field(..., ge=0, description="Skills matched to the healthcare taxonomy")
    unrecognized_count: int            = Field(..., ge=0, description="Free-form skills with no taxonomy match")
    recognized_ratio:   float          = Field(..., ge=0.0, le=1.0, description="recognized_count / total (0.0–1.0)")
    recognized:         list[str]      = Field(default_factory=list, description="Canonical names of recognized skills")
    unrecognized:       list[str]      = Field(default_factory=list, description="Skills not found in the taxonomy")
    groups:             dict[str, str] = Field(default_factory=dict, description="Recognized specialty → group label (e.g. 'Intensive Care Unit' → 'ICU')")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "total": 5,
                "recognized_count": 4,
                "unrecognized_count": 1,
                "recognized_ratio": 0.8,
                "recognized": [
                    "Intensive Care Unit",
                    "Neonatal Intensive Care Unit",
                    "Registered Nurse",
                    "ACLS",
                ],
                "unrecognized": ["Patient Advocacy"],
                "groups": {
                    "Intensive Care Unit": "ICU",
                    "Neonatal Intensive Care Unit": "Nursery",
                },
            }
        }
    )
