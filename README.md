# ParkEasy API

FastAPI backend for **ParkEasy Web** - intelligent parking discovery, reservation
and parking operations. Built from the *ParkEasy Web Client Requirement and
Project Plan* v1.0.

## Status: Phase 1 (Foundation)

Infrastructure only. The health probes, MongoDB connectivity, index bootstrap,
error envelope, CORS, WebSocket channel and the full documented route surface
are in place. **Every business endpoint returns `501 Not Implemented`** until
its phase lands. See `docs/ROADMAP.md` for the phase breakdown.

## Requirements

| Tool | Version | Notes |
| --- | --- | --- |
| Python | 3.14 | cp314 wheels verified for all dependencies |
| MongoDB | 7.0+ | local server, or an Atlas `mongodb+srv://` URI |
| Node.js | 22+ | only needed to run the `Client/` app |

## Setup

```powershell
cd Server

# 1. Virtual environment
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Dependencies
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt

# 3. Environment
Copy-Item .env.example .env
```

Then edit `Server/.env` and replace `JWT_SECRET` with a real value:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Run

```powershell
python run.py
# or
python -m app.main
# or
uvicorn app.main:app --port 9999 --reload
```

| URL | What |
| --- | --- |
| <http://127.0.0.1:9999/> | service banner |
| <http://127.0.0.1:9999/api/v1/health> | liveness |
| <http://127.0.0.1:9999/api/v1/health/db> | readiness (real MongoDB ping) |
| <http://127.0.0.1:9999/docs> | Swagger UI |
| <http://127.0.0.1:9999/ws> | live occupancy WebSocket |

## Verify

```powershell
ruff check .                                  # lint
python -m pytest                              # tests
Invoke-RestMethod http://127.0.0.1:9999/api/v1/health
Invoke-RestMethod http://127.0.0.1:9999/api/v1/health/db
```

Confirm the geospatial index from DOC section 19 exists:

```javascript
// mongosh
use parkeasy
db.parkingFacilities.getIndexes()
```

## Layout

```
app/
  main.py        application factory, lifespan, CORS, WebSocket channel
  core/          config (pydantic-settings), security (PyJWT + bcrypt), errors, logging
  db/            PyMongo AsyncMongoClient lifecycle, index definitions
  api/           dependencies (auth, RBAC, facility scope) and v1 routers
  models/        domain enums (DOC section 17)
  schemas/       shared Pydantic response envelopes
  services/      business logic - one module per domain (DOC section 24)
  sockets/       WebSocket connection manager + DOC section 26 event names
  jobs/          background jobs: expiry, reminders, prediction refresh
  utils/         MongoDB <-> API conversion helpers
tests/           pytest suite
```

## Conventions

- **Collections** keep the DOC section 18 names: `parkingFacilities`, `parkingSessions`, ...
- **Status values** are stored in the exact DOC section 17 spelling (`AVAILABLE`, `RESERVED`, ...).
  The client's lowercase literals are mapped at the API boundary.
- **Ids** are MongoDB `ObjectId`s on the server and strings over the wire.
- **Errors** always use one envelope: `{"error": {"code", "message", "details"}}`.
- **Auth** is a JWT access token sent as `Authorization: Bearer <token>`.
- **Routers** stay thin; all rules live in `app/services/`. Do not put business
  logic in route handlers (DOC section 47).
- **No** `passlib` (incompatible with bcrypt 5.x) and **no** `motor`
  (deprecated - use `pymongo.AsyncMongoClient`).

## Deviations from the requirements document

| Section | Document says | Here | Reason |
| --- | --- | --- | --- |
| 24, 25 | Node.js + Express, `app.js` / `server.js` | Python FastAPI, `app/main.py` | project decision |
| 26 | Socket.IO | native FastAPI WebSockets, same event names | fewer dependencies |
| 25 | Redis for locks and cache | MongoDB atomic operations for now | no Redis on this machine; revisit in Phase 3 |
