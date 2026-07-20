"""Benchmark runner: score parser output against the hand-labelled dataset.

Two modes for producing the *actual* parser output:

  --actual-dir DIR   score pre-saved payloads (default: benchmark/data/actual).
                     Each file is either a raw /resume/parse response or its
                     inner ``data`` object. This is how we baseline a deployment
                     without re-spending compute/quota.

  --api URL --key K  call a live parser for every resume in --resumes-dir,
                     saving each payload into --actual-dir first, then score.

  --local            parse every resume in --resumes-dir through the in-process
                     pipeline (app.services.pipeline.run) using local settings/keys,
                     saving each payload first, then score. No network endpoint or
                     API key needed - this is the fast inner loop for developing a
                     fix and re-scoring against the labelled set.

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
    ap.add_argument("--local", action="store_true",
                    help="parse --resumes-dir through the in-process pipeline (no endpoint/key needed)")
    ap.add_argument("--resumes-dir", help="folder of resume files to parse when --api/--local is set")
    ap.add_argument("--only", help="substring filter: only parse/score resumes whose stem contains this")
    ap.add_argument("--misses", action="store_true", help="list every individual miss")
    args = ap.parse_args(argv)

    expected_dir = Path(args.expected_dir)
    actual_dir = Path(args.actual_dir)
    actual_dir.mkdir(parents=True, exist_ok=True)

    if args.api:
        _parse_all(args.api, args.key, Path(args.resumes_dir), actual_dir)
    elif args.local:
        _parse_all_local(Path(args.resumes_dir), actual_dir, args.only)

    expected_files = sorted(expected_dir.glob("*.json"))
    if args.only:
        expected_files = [f for f in expected_files if args.only.lower() in f.stem.lower()]
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


def _pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile (p in 0..100). Empty -> 0.0."""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(p / 100.0 * (len(s) - 1))))
    return s[k]


def _perf_report(rows: list[dict]) -> None:
    """Print a latency + token-cost summary for a --local run.

    The accuracy scorer stays PII-free and timing-free; this is the companion
    speed/cost view, so a change can be judged on all three axes (accuracy, latency,
    cost) instead of accuracy alone.
    """
    ok = [r for r in rows if r["status"] == "completed"]
    if not ok:
        print("\n(no completed parses to summarise for perf)")
        return
    lat = [r["duration_ms"] / 1000.0 for r in ok]
    toks = [r["tokens"] for r in ok]
    roles = sum(r["roles"] for r in ok) or 1
    print("\nperformance (completed parses):")
    print(f"  resumes           {len(ok)}  ({len(rows) - len(ok)} failed)")
    print(f"  latency  s        p50 {_pct(lat, 50):.1f}   p95 {_pct(lat, 95):.1f}   "
          f"max {max(lat):.1f}   mean {sum(lat)/len(lat):.1f}")
    print(f"  tokens / resume   p50 {_pct([float(t) for t in toks], 50):.0f}   "
          f"mean {sum(toks)/len(toks):.0f}   total {sum(toks)}")
    print(f"  tokens / role     {sum(toks)/roles:.0f}")
    partials = sum(1 for r in ok if r["partial"])
    if partials:
        print(f"  partial records   {partials}/{len(ok)}")


def _parse_all_local(resumes_dir: Path, actual_dir: Path, only: str | None = None) -> None:
    """Parse every resume in `resumes_dir` through the in-process pipeline.

    Uses the same code path as the async worker (sync=False -> full budget,
    orchestrator-first), so scores reflect exactly what production would return.
    Imported lazily so the pure scoring path stays dependency-free.
    """
    import asyncio
    import uuid

    from app.services.pipeline import PipelineInput, run

    files = [f for f in sorted(resumes_dir.iterdir())
             if f.is_file() and (not only or only.lower() in f.stem.lower())]
    perf: list[dict] = []

    async def _one(f: Path) -> None:
        inp = PipelineInput(
            job_id=uuid.uuid4().hex, filename=f.name,
            content=f.read_bytes(), company_id="benchmark", sync=False,
        )
        try:
            res = await run(inp)
            n_exp = len(res.parsed.experience)
            payload = {
                "job_id": inp.job_id, "status": "completed",
                "data": res.parsed.model_dump(mode="json"),
                "confidence": res.confidence.model_dump(mode="json"),
                "partial": res.partial, "warnings": res.warnings,
                # Persisted so a later --actual-dir re-score can still see the cost.
                "perf": {"duration_ms": res.duration_ms, "ai_tokens_used": res.ai_tokens_used},
            }
            perf.append({"status": "completed", "duration_ms": res.duration_ms,
                         "tokens": res.ai_tokens_used, "roles": n_exp, "partial": res.partial})
            print(f"parsed {f.name} -> {n_exp} roles, conf {res.confidence.overall:.2f}, "
                  f"{res.duration_ms/1000:.0f}s, {res.ai_tokens_used} tok, warnings={len(res.warnings)}")
        except Exception as exc:  # noqa: BLE001 — record the failure, keep going
            payload = {"job_id": inp.job_id, "status": "failed", "error": str(exc)}
            perf.append({"status": "failed", "duration_ms": 0, "tokens": 0, "roles": 0, "partial": False})
            print(f"parsed {f.name} -> FAILED: {exc}")
        (actual_dir / f"{f.stem}.json").write_text(json.dumps(payload), encoding="utf-8")

    async def _all() -> None:
        # Serialise files (each parse already fans out internally under its own
        # semaphore); parallelising whole resumes would multiply burst TPM.
        for f in files:
            await _one(f)

    asyncio.run(_all())
    _perf_report(perf)


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
