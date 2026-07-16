"""Benchmark runner: score parser output against the hand-labelled dataset.

Two modes for producing the *actual* parser output:

  --actual-dir DIR   score pre-saved payloads (default: benchmark/data/actual).
                     Each file is either a raw /resume/parse response or its
                     inner ``data`` object. This is how we baseline a deployment
                     without re-spending compute/quota.

  --api URL --key K  call a live parser for every resume in --resumes-dir,
                     saving each payload into --actual-dir first, then score.

Expected labels live in benchmark/data/expected/<name>.json and are matched to an
actual payload by the file stem. Everything under benchmark/data/ is gitignored
(candidate PII); this script and the scorer carry none.

    python -m benchmark.run
    python -m benchmark.run --api https://api.parsinglab.blue-iq.ai/api/v1/resume/parse --key rp_live_... --resumes-dir "/path/to/Resumes"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from benchmark.scorer import Score, score_resume

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"


def _load(path: Path) -> dict:
    obj = json.loads(path.read_text(encoding="utf-8-sig"))
    # Accept either a full response envelope or the bare data object.
    return obj.get("data", obj) if isinstance(obj, dict) else {}


def _report(per_resume: list[tuple[str, Score]], total: Score, show_misses: bool) -> None:
    metric_order = [
        "contact", "credentials", "role_recall", "role_precision",
        "date_accuracy", "city_resolution", "geo_ids", "education", "negatives",
    ]
    print(f"\n{'resume':<26}" + "".join(f"{m[:9]:>11}" for m in metric_order) + f"{'score':>9}")
    print("-" * (26 + 11 * len(metric_order) + 9))
    for name, s in per_resume:
        cells = ""
        for m in metric_order:
            r = s.rate(m)
            cells += f"{'-':>11}" if r is None else f"{r*100:>10.0f}%"
        ov = s.overall()
        print(f"{name:<26}" + cells + (f"{ov*100:>8.0f}%" if ov is not None else f"{'-':>9}"))
    print("-" * (26 + 11 * len(metric_order) + 9))
    cells = ""
    for m in metric_order:
        r = total.rate(m)
        h, t = total.metrics.get(m, [0, 0])
        cells += f"{'-':>11}" if r is None else f"{r*100:>10.0f}%"
    ov = total.overall()
    print(f"{'TOTAL':<26}" + cells + (f"{ov*100:>8.0f}%" if ov is not None else f"{'-':>9}"))

    print("\nper-metric (hits/total):")
    for m in metric_order:
        h, t = total.metrics.get(m, [0, 0])
        if t:
            print(f"  {m:<16} {h}/{t}  ({h/t*100:.0f}%)")

    if show_misses and total.misses:
        print(f"\nmisses ({len(total.misses)}):")
        for miss in total.misses:
            print(f"  - {miss}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score the resume parser against the labelled benchmark.")
    ap.add_argument("--actual-dir", default=str(DATA / "actual"))
    ap.add_argument("--expected-dir", default=str(DATA / "expected"))
    ap.add_argument("--api", help="live parse endpoint; when set, each resume is parsed and saved before scoring")
    ap.add_argument("--key", help="X-API-Key for --api")
    ap.add_argument("--resumes-dir", help="folder of resume files to parse when --api is set")
    ap.add_argument("--misses", action="store_true", help="list every individual miss")
    args = ap.parse_args(argv)

    expected_dir = Path(args.expected_dir)
    actual_dir = Path(args.actual_dir)
    actual_dir.mkdir(parents=True, exist_ok=True)

    if args.api:
        _parse_all(args.api, args.key, Path(args.resumes_dir), actual_dir)

    expected_files = sorted(expected_dir.glob("*.json"))
    if not expected_files:
        print(f"No expected labels in {expected_dir}. See benchmark/README.md.", file=sys.stderr)
        return 2

    per_resume: list[tuple[str, Score]] = []
    total = Score()
    missing = []
    for ef in expected_files:
        af = actual_dir / ef.name
        if not af.exists():
            missing.append(ef.stem)
            continue
        s = score_resume(_load(af), json.loads(ef.read_text(encoding="utf-8-sig")))
        per_resume.append((ef.stem, s))
        total.merge(s)

    _report(per_resume, total, args.misses)
    if missing:
        print(f"\n(no actual payload for: {', '.join(missing)})", file=sys.stderr)
    return 0


def _parse_all(api: str, key: str, resumes_dir: Path, actual_dir: Path) -> None:
    """Parse every resume in `resumes_dir` via the live API, saving each payload.

    Imported lazily so the offline scoring path needs no httpx/network.
    """
    import time

    import httpx

    for f in sorted(resumes_dir.iterdir()):
        if not f.is_file():
            continue
        with httpx.Client(timeout=120) as client:
            with f.open("rb") as fh:
                resp = client.post(api, headers={"X-API-Key": key}, files={"file": (f.name, fh)})
        body = resp.json()
        # Poll async jobs to completion.
        job_id, status = body.get("job_id"), body.get("status")
        while status == "processing" and job_id:
            time.sleep(8)
            with httpx.Client(timeout=60) as client:
                body = client.get(f"{api.rsplit('/', 1)[0]}/job/{job_id}",
                                  headers={"X-API-Key": key}).json()
            status = body.get("status")
        (actual_dir / f"{f.stem}.json").write_text(json.dumps(body), encoding="utf-8")
        print(f"parsed {f.name} -> {status}")


if __name__ == "__main__":
    raise SystemExit(main())
