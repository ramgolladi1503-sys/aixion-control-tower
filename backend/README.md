# Aixion Control Tower Backend

FastAPI MVP backend for projects, ideas, work orders, approvals, risk scoring, test records, and audit logs.

## Run Locally

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:app --reload
```

API docs:

```text
http://127.0.0.1:8000/docs
```

## Run Tests

```bash
cd backend
pip install -e '.[dev]'
pytest
```

## MVP Endpoints

```text
GET  /health
POST /projects
GET  /projects
POST /ideas
GET  /ideas
POST /work-orders
GET  /work-orders
POST /approvals
GET  /approvals
GET  /approvals/{approval_id}
POST /approvals/{approval_id}/decision
POST /test-runs
GET  /test-runs
GET  /audit
```

## Current Storage

The MVP uses an in-memory store so the workflow can be validated quickly. A real database layer should be added before production use.

## Safety Behavior

The backend blocks:

- direct protected branch requests
- sensitive file paths
- missing diffs
- approval of blocked requests
- approval of high-risk requests without required actions
