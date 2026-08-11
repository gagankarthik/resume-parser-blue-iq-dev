"""ValidatorAgent - Stage 4: reconcile extracted bullets against the structure map.

For each role whose extracted description-bullet count doesn't match the count
the StructureAgent found, re-extract that single role with an explicit count.
Unlike the general-purpose engine this is ported from, we do NOT drop a
mismatched role - losing a healthcare assignment (and its dates/licence context)
is worse than keeping a slightly-off bullet list. We keep the better of the two
bullet lists (the one closer to the expected count) and flag the residual.

The re-extraction is MERGED into the first pass, never substituted for it: it is
authoritative for the duty list only, so a field its narrower prompt omitted
(specialties, employer_phone, shift ...) survives the correction.
"""

from __future__ import annotations

import asyncio

from app.core.logging import get_logger
from app.models.schemas import ExperienceItem

from .base import BaseAgent, TokenMeter
from .prompts import CORE_RULES
from .schemas import RoleBoundary
from .work import apply_role_boundary

log = get_logger(__name__)

_SYSTEM = f"""You RE-EXTRACT one work-history role from a healthcare resume - the previous pass had the wrong number of responsibility bullets.

{CORE_RULES}

This role has EXACTLY {{expected}} responsibility bullet(s). Extract all {{expected}} into description[], verbatim, one per item - do NOT add, skip, or merge any. Also return every other field you can find (company, role, dates, location, city/state/zip, employer_phone, profession, specialties, agency_name, shift, charting_system, achievements ...) using the same schema, so nothing from the first pass is lost. specialties is a list of OBJECTS each with just a name (e.g. [{{{{"name": "ICU"}}}}]) - fill only `name`.

EMBEDDED CITY: if the role has no separate address but its facility NAME contains a real US city/town, set `city` to that place ("Elderwood of Lockport" -> "Lockport"); leave `state` null (the system infers it). NEVER mine a state, region, hospital-system, university, or person's name as a city."""


def _bullets(item: ExperienceItem) -> int:
    return len(item.description)


# The re-extraction is authoritative for these and only these: it was re-run
# precisely to get the duty list right. Every other field stays with the first
# pass, whose prompt is the wider of the two.
_REEXTRACT_OWNS = ("description", "achievements")


def _is_blank(value: object) -> bool:
    """True for a field the first pass left genuinely unset (None/""/[]).

    Deliberately excludes False and 0.0: a bool flag or an id-confidence that the
    first pass set is real data, not a blank to be overwritten.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list | dict):
        return not value
    return False


def _merge_reextraction(base: ExperienceItem, res: ExperienceItem) -> ExperienceItem:
    """Adopt the re-extraction's duty list without discarding the first pass.

    The re-extraction exists ONLY to fix the bullet count, and its prompt is
    narrower than the WorkAgent's. Swapping the whole item for it silently drops
    anything the narrower pass happened to omit - specialties, employer_phone,
    shift, charting_system, bed counts - and no warning fires when the corrected
    count matches, so the loss is invisible. So: start from `base`, take the
    re-extracted duty list, and let `res` fill ONLY the fields `base` left blank.
    """
    merged = base.model_copy(deep=True)
    merged.description = list(res.description)
    if res.achievements:
        merged.achievements = list(res.achievements)

    for name in type(base).model_fields:
        if name in _REEXTRACT_OWNS:
            continue
        if _is_blank(getattr(merged, name)):
            candidate = getattr(res, name)
            if not _is_blank(candidate):
                setattr(merged, name, candidate)
    return merged


class ValidatorAgent(BaseAgent):
    name = "ValidatorAgent"

    async def run(
        self,
        work: list[ExperienceItem],
        roles: list[RoleBoundary],
        text: str,
        meter: TokenMeter,
    ) -> tuple[list[ExperienceItem], list[str]]:
        """Return (possibly-corrected work list, warnings)."""
        warnings: list[str] = []
        # Pair work items with role boundaries positionally; only validate the
        # overlap (per-role extraction preserves order 1:1 with the structure map).
        mismatches = [
            (i, roles[i])
            for i in range(min(len(work), len(roles)))
            if roles[i].bullet_count > 0 and _bullets(work[i]) != roles[i].bullet_count
        ]
        if not mismatches:
            return work, warnings

        log.info("validator_reextract", count=len(mismatches))
        results = await asyncio.gather(
            *[self._reextract(text, role, meter) for _, role in mismatches],
            return_exceptions=True,
        )

        for (idx, role), res in zip(mismatches, results):
            expected = role.bullet_count
            if isinstance(res, Exception) or res is None:
                warnings.append(
                    f"Role '{work[idx].company}' may have an incomplete duty list "
                    f"(expected {expected} bullets, got {_bullets(work[idx])})."
                )
                continue
            # Keep whichever extraction is closer to the expected bullet count -
            # merging, not replacing, so a field the re-extraction omitted is not
            # silently lost along with the bullet fix (see `_merge_reextraction`).
            if abs(_bullets(res) - expected) < abs(_bullets(work[idx]) - expected):
                work[idx] = _merge_reextraction(work[idx], res)
            if _bullets(work[idx]) != expected:
                warnings.append(
                    f"Role '{work[idx].company}': extracted {_bullets(work[idx])} "
                    f"of {expected} expected duty bullets - review."
                )
        return work, warnings

    async def _reextract(self, text: str, role: RoleBoundary, meter: TokenMeter) -> ExperienceItem:
        system = _SYSTEM.format(expected=role.bullet_count)
        user = (
            f"Role: {role.company} | {role.title or ''} | "
            f"{role.start_date or ''}-{role.end_date or ''}\n\n"
            f"=== RESUME TEXT ===\n{text}\n=== END ===\n\nReturn ONLY this single role."
        )
        item = await self._structured_call(system, user, ExperienceItem, meter, max_tokens=4096)
        # Same structure-map reconciliation as the WorkAgent's first pass, so a
        # re-extraction can never lose the agency/profession seeding or have the
        # staffing agency displace the facility in `company`.
        return apply_role_boundary(item, role)
