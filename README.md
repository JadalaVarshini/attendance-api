# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+; `$setWindowFields` and `$rank` need it). All code is in `app/main.py`.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # or export MONGO_URI / MONGO_DB (real env variables win over .env)
python sample_seed.py             # optional: loads the nine sample documents
uvicorn app.main:app --port 8000
```

Indexes are created idempotently at startup. Interactive docs: `http://localhost:8000/docs`.

- `REVIEW.md` lists the defects found in the starter and how each was fixed.
- `DECISIONS.md` answers the five design questions.

## Notes for the reviewer

- **Not run against a real MongoDB.** The sandbox I built this in could not reach a MongoDB binary. The CRUD endpoints, the R1-R5 rules, validation and the concurrency paths were tested against `mongomock`. The three analytics pipelines and `/admin/explain` were only checked to be well-formed BSON and reviewed by hand. Please run them once against a real 6.0+/7.0 instance before relying on them.
- Trend gap-filling uses `$facet` + `$range` instead of `$densify`, so a range with no logs at all still yields every day. Everything is still computed in the database.
- Trend `moving_avg_7d` averages the already-rounded (4 decimal) daily rates, which is how I read "the non-null `attendance_rate` values".
- The trend range limit is 92 calendar days inclusive; 93 days or more returns 422.
- `PATCH` retries up to 8 times on a concurrent modification before answering 409, so concurrent corrections each get their own history entry.
- A punch-in on a date that already has a record of any kind (including a seeded `ABSENT` or `LEAVE`) returns 409.
