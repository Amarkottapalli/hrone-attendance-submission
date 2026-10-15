# Employee Attendance & Analytics API

FastAPI + PyMongo implementation of the HROne attendance assignment.

## Requirements

- Python 3.11+
- MongoDB 6.0+
- Git

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'
uvicorn app.main:app --port 8000
```

The application loads `.env` for local convenience, while actual environment variables take precedence. Do not commit `.env` or secrets. Indexes are created idempotently at startup. `GET /health` pings MongoDB.

To load the supplied sample documents, configure the same environment variables and run:

```bash
python sample_seed.py
```

Interactive API documentation is available at `/docs`.

## Required files

- `app/main.py` — API implementation
- `requirements.txt` — runtime dependencies
- `REVIEW.md` — review of the starter defects
- `DECISIONS.md` — design decisions
- `DATA_MODEL.md`, `openapi.yaml`, `sample_data/` — assignment reference material

## Tests

With the dependencies installed, run `pytest -q`. The focused helper tests cover grace-period boundaries, half-up work-hour rounding, overtime threshold, and an overnight attendance date. Endpoint and aggregation tests still need to be run against a real MongoDB 6.0+ instance.

## Before submission

1. Run the API against MongoDB 6.0+ and exercise each endpoint.
2. Test edge cases: duplicate parallel punch-ins, simultaneous punch-outs, overnight shifts, weekend records, joiners mid-month, and tied leaderboard ranks.
3. Verify `/admin/explain/{endpoint}` plans against the actual dataset.
4. Do not commit `.env`, secrets, a Dockerfile, virtual environments, or local database files.

## Known limitations to verify

The assignment's hidden dataset and exact MongoDB execution plans are not available in this workspace. Run the included app against MongoDB 6.0+ and validate every aggregation and explain plan before submission; do not treat compilation alone as proof of correctness. Any remaining gaps should be stated honestly in the final submission.
