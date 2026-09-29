# ParkEasy API

FastAPI backend for **ParkEasy Web** - intelligent parking discovery, reservation
and parking operations. Built from the *ParkEasy Web Client Requirement and
Project Plan* v1.0.

## Status: Phase 1 (Foundation + Authentication)

The health probes, MongoDB connectivity, index bootstrap, error envelope, CORS
and WebSocket channel are in place, and the full route surface is documented.

**Authentication is live** for all four roles from DOC section 12 - register,
login, current-user and logout. **Every other business endpoint still returns
`501 Not Implemented`** until its phase lands.

| Area | State |
| --- | --- |
| Health, MongoDB, indexes, CORS, errors, WebSocket | done |
| `auth` - register / login / me / logout | done |
| `users` profile, vehicles, parking, spaces, reservations, ... | `501` |
| `forgot-password` / `reset-password` (DOC section 12) | not started |

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

## Authentication

Four endpoints under `/api/v1/auth`. Register and login both return a signed
access token; send it as `Authorization: Bearer <token>` on protected routes.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/api/v1/auth/register` | none | create an account, returns a token |
| `POST` | `/api/v1/auth/login` | none | exchange credentials for a token |
| `GET` | `/api/v1/auth/me` | Bearer | the current user |
| `POST` | `/api/v1/auth/logout` | Bearer | revoke this token |

```powershell
# Register a driver
Invoke-RestMethod http://127.0.0.1:9999/api/v1/auth/register -Method Post `
  -ContentType 'application/json' `
  -Body '{"name":"Aarav Shah","email":"aarav@example.com","phone":"+919876543210","password":"parkeasy123","role":"driver"}'

# Log in
$login = Invoke-RestMethod http://127.0.0.1:9999/api/v1/auth/login -Method Post `
  -ContentType 'application/json' `
  -Body '{"email":"aarav@example.com","password":"parkeasy123"}'
$headers = @{ Authorization = "Bearer $($login.accessToken)" }

Invoke-RestMethod http://127.0.0.1:9999/api/v1/auth/me -Headers $headers
Invoke-RestMethod http://127.0.0.1:9999/api/v1/auth/logout -Method Post -Headers $headers
```

`role` accepts `driver`, `staff`, `operator` or `admin`. Passwords must be at
least 8 characters and contain a letter and a digit. Emails are stored
lowercased and are unique.

### How it works

- **Passwords** are hashed with `bcrypt` (cost 12) via `app/core/security.py`.
  The plain password is never stored or returned. Inputs longer than bcrypt's
  72-byte limit are pre-hashed with SHA-256 so long passphrases still work.
- **Tokens** are HS256 JWTs carrying `sub`, `role`, `email`, `name`, `status`,
  `facilityIds` and a unique `jti`, valid for `JWT_EXPIRES_MINUTES` (default 60).
  There is no refresh token yet.
- **Logout is real.** The token's `jti` is written to the `revokedTokens`
  collection, and `get_current_user` rejects it on every subsequent request.
  A TTL index on `exp` deletes each row once the token would have expired
  anyway, so no sweeper job is needed. Other sessions of the same user keep
  working - each login gets its own `jti`.
- **Login failures are indistinguishable**: an unknown email and a wrong
  password both return the same `INVALID_CREDENTIALS` 401, and a miss still
  runs a dummy bcrypt comparison so response timing does not reveal which
  addresses are registered (DOC section 31, user enumeration).
- **Suspended and pending** accounts are refused with `403` and a distinct
  `ACCOUNT_SUSPENDED` / `ACCOUNT_PENDING` code.

### Configuration

| Setting | Default | Notes |
| --- | --- | --- |
| `JWT_SECRET` | placeholder | **change this**; generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `JWT_EXPIRES_MINUTES` | `60` | access token lifetime |
| `SELF_REGISTER_ROLES` | `driver,staff,operator,admin` | which roles may self-register |
| `LOGIN_RATE_LIMIT` / `LOGIN_RATE_WINDOW_SECONDS` | `10` / `60` | per IP+email |
| `REGISTER_RATE_LIMIT` / `REGISTER_RATE_WINDOW_SECONDS` | `5` / `3600` | per IP+email |

> **Before deploying:** set `SELF_REGISTER_ROLES=driver`. With the default,
> anyone who can reach the API can create an `admin` account. The app logs a
> `SECURITY:` warning on every boot while a privileged role is self-registerable,
> and logs it at `CRITICAL` when `ENVIRONMENT=production`.

Rate limiting is an in-process counter (`app/core/rate_limit.py`), which is
fine for the single Uvicorn worker used here. Move it to Redis before running
more than one process, or the limit is per-process and trivially multiplied.

### Not yet implemented

- `POST /auth/forgot-password` and `POST /auth/reset-password` (DOC section 12).
  Needs an email transport; there is none configured.
- Refresh tokens and session listing.
- Email verification.
- Admin approval for operator onboarding. New accounts are `ACTIVE`
  immediately; flip the default in `app/services/auth.py` when that lands.
- WebSocket `/ws` still accepts unauthenticated connections.

## Layout

```
app/
  main.py        application factory, lifespan, CORS, WebSocket channel
  core/          config (pydantic-settings), security (PyJWT + bcrypt), errors,
                 logging, rate limiting
  db/            PyMongo AsyncMongoClient lifecycle, index definitions
  api/           dependencies (auth, RBAC, facility scope) and v1 routers
  models/        domain enums (DOC section 17), user document shape
  schemas/       shared envelopes plus the auth request/response models
  services/      business logic - one module per domain (DOC section 24)
  sockets/       WebSocket connection manager + DOC section 26 event names
  jobs/          background jobs: expiry, reminders, prediction refresh
  utils/         MongoDB <-> API conversion helpers
tests/
  conftest.py    offline app fixture + MongoDB-backed auth fixture
  test_health.py routing, envelopes, configuration, enums
  test_auth.py   auth/RBAC dependencies in isolation
  test_auth_flow.py  register/login/me/logout against a real MongoDB
```

## Conventions

- **Collections** keep the DOC section 18 names: `parkingFacilities`, `parkingSessions`, ...
- **Status values** are stored in the exact DOC section 17 spelling (`AVAILABLE`, `RESERVED`, ...).
  The client's lowercase literals are mapped at the API boundary.
- **Ids** are `ObjectId`s on the server and 24-character hex strings over the
  wire. `app/utils/mongo.py` holds `to_object_id()` for parsing path params and
  `serialize()` for converting documents to JSON. Let MongoDB generate `_id` on
  insert rather than inventing one; `insert_one` populates it in place.
- **Errors** always use one envelope: `{"error": {"code", "message", "details"}}`.
- **Auth** is a JWT access token sent as `Authorization: Bearer <token>`, plus
  the `revokedTokens` denylist for logout. No refresh token in Phase 1.
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
| 12 | four roles, role chosen at login | `role` is a field on the account; login takes email + password only | the role decides the dashboard, so it must be a server-side property rather than a picker on the login form |
| 12 | admin approves operator onboarding | all roles self-register and start `ACTIVE` | requested for a four-role demo; governed by `SELF_REGISTER_ROLES` |
