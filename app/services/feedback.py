"""
Feedback diffing - compares the original parser JSON with the user-corrected
JSON and returns the leaf fields that changed.

The diff drives the `changed` flag and the `changed_fields` list returned by the
feedback endpoint, and is what makes the stored feedback useful for model
improvement (it pinpoints exactly which parser outputs the user had to correct).

Paths use dotted keys for objects and `[i]` for list indices, e.g.
  personal_info.full_name
  experience[0].title
  skills[2]
"""

from typing import Any

# Cap recursion so a pathologically nested payload can't blow the stack.
_MAX_DEPTH = 25


def diff_fields(original: Any, updated: Any, prefix: str = "", _depth: int = 0) -> list[str]:
    """Return the dotted paths of every leaf value that differs between two
    JSON-like structures (dicts, lists, scalars).

    Missing keys/indices are treated as a change to/from ``None``.
    """
    if _depth >= _MAX_DEPTH:
        # Too deep to keep descending - compare what's left as opaque values.
        return [prefix or ""] if original != updated else []

    if isinstance(original, dict) or isinstance(updated, dict):
        orig = original if isinstance(original, dict) else {}
        upd = updated if isinstance(updated, dict) else {}
        changed: list[str] = []
        for key in sorted(set(orig) | set(upd), key=str):
            path = f"{prefix}.{key}" if prefix else str(key)
            changed += diff_fields(orig.get(key), upd.get(key), path, _depth + 1)
        return changed

    if isinstance(original, list) or isinstance(updated, list):
        o = original if isinstance(original, list) else []
        u = updated if isinstance(updated, list) else []
        changed = []
        for i in range(max(len(o), len(u))):
            path = f"{prefix}[{i}]"
            ov = o[i] if i < len(o) else None
            uv = u[i] if i < len(u) else None
            changed += diff_fields(ov, uv, path, _depth + 1)
        return changed

    return [prefix] if original != updated else []
