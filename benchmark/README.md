# Resume-parser accuracy benchmark

A small, honest measurement harness: score parser output against a hand-labelled
set of resumes so we can track recall/precision over time and prove a change helps
(or regresses) instead of eyeballing one resume at a time.

## What is committed vs. not

- **Committed** (no PII): `scorer.py`, `run.py`, `__init__.py`, this README, and the
  scorer's unit test (`tests/unit/test_benchmark_scorer.py`, synthetic data only).
- **Gitignored** (`benchmark/data/`): the resumes and the `expected/` labels — they
  contain candidate names, emails and phones. This matches the project's rule that
  resume content never lives in the repo.

## Layout

```
benchmark/
  scorer.py               pure scoring logic (hits/total per metric + misses)
  run.py                  CLI: score, or parse-then-score against a live endpoint
  data/                   (gitignored)
    expected/<name>.json  hand-labelled truth, one file per resume
    actual/<name>.json    parser output to score (saved /resume/parse payloads)
```

`expected/<name>.json` is matched to `actual/<name>.json` by filename stem.

## Run it

Score already-saved payloads (the default — baseline a deployment cheaply):

```bash
python -m benchmark.run                # summary table
python -m benchmark.run --misses       # + every individual miss
```

Parse a folder of resumes through a live endpoint, then score (spends compute/quota;
async jobs are polled to completion and each payload is saved to `data/actual/`):

```bash
python -m benchmark.run \
  --api https://api.parsinglab.blue-iq.ai/api/v1/resume/parse \
  --key rp_live_... \
  --resumes-dir "/path/to/Resumes"
```

## Metrics

Each is a recall-style `hits/total`; `overall` is their unweighted mean.

| Metric | Measures |
|---|---|
| `contact` | name / email / phone correct (normalised compare) |
| `credentials` | post-nominals recalled |
| `role_recall` | expected work-history roles found (company **or** role match) |
| `role_precision` | output roles that map to a real expected role (catches hallucinated/duplicated roles) |
| `date_accuracy` | matched roles with correct start **and** end dates |
| `city_resolution` | roles whose city IS in GigHealth's catalog that got a `city_id` |
| `geo_ids` | matched roles with the correct `state_id` |
| `education` | expected institutions found |
| `negatives` | false positives that must **not** appear (phantom phone, spurious specialty id) |

`city_resolution` counts only roles the label marks `catalog_city: true`. A city
GigHealth simply does not carry (e.g. Opelousas, LA) is `false`, so a genuine catalog
gap is never charged against the parser — only extraction/resolution gaps the parser
can actually close.

## Labelling a new resume

Add `data/expected/<stem>.json`:

```json
{
  "personal_info": {"full_name": "...", "email": "...", "phone_digits": "5551234567",
                    "credentials": ["RN"]},
  "experience": [
    {"company_key": "united memorial", "start_date": "08/2021", "end_date": "Present",
     "city": "Batavia", "catalog_city": true, "state_id": "35"}
  ],
  "education_keys": ["niagara university"],
  "negatives": {"no_phone_secondary_digits": ["9501200"]}
}
```

- `company_key` is a distinctive lowercase substring of the company **or** role.
- Omit any field you don't want scored (e.g. leave out `start_date` to skip it).
- `phone_digits` is the true number with non-digits stripped.
