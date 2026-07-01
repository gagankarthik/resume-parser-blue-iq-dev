#!/usr/bin/env python
"""
Regenerate the bundled specialty catalog snapshot from the GigHealth API.

    python -m scripts.refresh_specialty_catalog          # uses env / settings
    python -m scripts.refresh_specialty_catalog --dry-run
    GIG_SPECIAILITIES_API_KEY=... python -m scripts.refresh_specialty_catalog

Fetches the platform's specialty taxonomy, flattens it into the flat catalog rows
the matcher consumes (id, specialty, full_name, group, profession, keywords[]) with
the platform's EXACT names, and writes them to app/data/specialty_catalog.json —
the snapshot the parser loads at runtime. Run this whenever the platform's
specialties change, then commit the regenerated JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as a plain script (python scripts/refresh_specialty_catalog.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.services.normalization import specialty_api  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "app" / "data" / "specialty_catalog.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"Output JSON path (default: {DEFAULT_OUT})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + summarise but do not write the file")
    args = parser.parse_args(argv)

    settings = get_settings()
    api_key = settings.gig_specialties_api_key
    if not api_key:
        print(
            "ERROR: no API key. Set GIG_SPECIAILITIES_API_KEY in .env or the "
            "environment.",
            file=sys.stderr,
        )
        return 2

    print(f"Fetching {settings.gig_specialties_api_url} ...", file=sys.stderr)
    payload = specialty_api.fetch_payload(settings.gig_specialties_api_url, api_key)
    rows = specialty_api.flatten_payload(payload)
    if not rows:
        print("ERROR: API returned no specialties; refusing to overwrite the "
              "snapshot.", file=sys.stderr)
        return 1

    professions = sorted({r["profession"] for r in rows if r["profession"]})
    with_keywords = sum(1 for r in rows if r["keywords"])
    print(
        f"Flattened {len(rows)} specialties across {len(professions)} professions "
        f"({with_keywords} carry curated keywords).",
        file=sys.stderr,
    )

    if args.dry_run:
        print("--dry-run: not writing.", file=sys.stderr)
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"specialties": rows}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
