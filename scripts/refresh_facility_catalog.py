#!/usr/bin/env python
"""
Regenerate the bundled facility catalog snapshot from the GigHealth API.

    python -m scripts.refresh_facility_catalog          # uses env / settings
    python -m scripts.refresh_facility_catalog --dry-run
    GIG_SPECIAILITIES_API_KEY=... python -m scripts.refresh_facility_catalog

Fetches the platform's facility directory and writes the flat catalog rows the
matcher consumes (id, name, health_system, health_system_id) - with the platform's
EXACT names - to app/data/facility_catalog.json, the snapshot the parser loads at
runtime. Run this whenever the platform's facilities change, then commit the
regenerated JSON. Authenticates with the same platform key as the specialties
refresh (GIG_SPECIAILITIES_API_KEY).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as a plain script (python scripts/refresh_facility_catalog.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.services.normalization import facility_api  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "app" / "data" / "facility_catalog.json"


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

    print(f"Fetching {settings.gig_facilities_api_url} ...", file=sys.stderr)
    payload = facility_api.fetch_payload(settings.gig_facilities_api_url, api_key)
    rows = facility_api.flatten_payload(payload)
    if not rows:
        print("ERROR: API returned no facilities; refusing to overwrite the "
              "snapshot.", file=sys.stderr)
        return 1

    health_systems = sorted({r["health_system"] for r in rows if r["health_system"]})
    print(
        f"Flattened {len(rows)} facilities across {len(health_systems)} health "
        f"systems.",
        file=sys.stderr,
    )

    if args.dry_run:
        print("--dry-run: not writing.", file=sys.stderr)
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"facilities": rows}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
