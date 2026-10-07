# ParkEasy API

FastAPI backend for **ParkEasy Web** - intelligent parking discovery, reservation
and parking operations. Built from the *ParkEasy Web Client Requirement and
Project Plan* v1.0.

## Status: Phase 1 (Foundation + Authentication)

The health probes, MongoDB connectivity, index bootstrap, error envelope, CORS
and WebSocket channel are in place, and the full route surface is documented.

**Authentication is live** for all four roles from DOC section 12 - register,
login, current-user and logout. **Profile and vehicles are live** for the
signed-in user. **Every other business endpoint still returns
`501 Not Implemented`** until its phase lands.

| Area | State |
| --- | --- |
| Health, MongoDB, indexes, CORS, errors, WebSocket | done |
| `auth` - register / login / me / logout | done |
| `auth` - forgot-password / reset-password / change-password | done |
| `users` - the signed-in user's own profile and photo | done |
| `vehicles` - list / create / edit / delete / set default | done |
| Remaining DOC section 21 groups (parking, spaces, reservations, ...) | `501` |

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
python -m pytest                              # tests (needs MongoDB)
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

Seven endpoints under `/api/v1/auth`. Register and login both return a signed
access token; send it as `Authorization: Bearer <token>` on protected routes.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/api/v1/auth/register` | none | create an account, returns a token |
| `POST` | `/api/v1/auth/login` | none | exchange credentials for a token |
| `GET` | `/api/v1/auth/me` | Bearer | the current user |
| `POST` | `/api/v1/auth/logout` | Bearer | revoke this token |
| `POST` | `/api/v1/auth/forgot-password` | none | email a single-use reset link |
| `POST` | `/api/v1/auth/reset-password` | none | redeem that link, set a new password |
| `POST` | `/api/v1/auth/change-password` | Bearer | rotate your own password |

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

### Password reset and change

```powershell
# 1. Ask for a link. Always 202 with the same body, whether or not the
#    address is registered.
$req = Invoke-RestMethod http://127.0.0.1:9999/api/v1/auth/forgot-password -Method Post `
  -ContentType 'application/json' -Body '{"email":"aarav@example.com"}'

# 2. In development the link comes back in the response instead of an inbox.
$token = ($req.devResetLink -split 'token=')[1]

# 3. Redeem it.
Invoke-RestMethod http://127.0.0.1:9999/api/v1/auth/reset-password -Method Post `
  -ContentType 'application/json' `
  -Body "{`"token`":`"$token`",`"newPassword`":`"newsecret456`"}"

# Or, while signed in, change your own password (needs the current one).
Invoke-RestMethod http://127.0.0.1:9999/api/v1/auth/change-password -Method Post `
  -Headers $headers -ContentType 'application/json' `
  -Body '{"currentPassword":"parkeasy123","newPassword":"newsecret456"}'
```

**How it works**

- **Tokens are single-use.** A 32-byte `secrets` token is emailed as
  `/reset-password?token=...`. Redemption is one `find_one_and_delete` on the
  token's hash, which atomically checks it, checks `exp`, and removes it - so a
  race between two redemptions cannot both succeed. Redeeming also deletes any
  sibling tokens, so the newest email is always the only one that works.
- **Only the hash is stored.** `_id` is the SHA-256 of the token, never the token
  itself, so a dump of `passwordResetTokens` cannot be replayed. A TTL index on
  `exp` clears links the user never clicked.
- **A password change kills every session.** `users.passwordChangedAt` is
  written in the same update as `passwordHash`, and `get_current_user` rejects
  any token issued before it. That covers the session that made the change too,
  so the client is redirected to sign in again. This is stateless - no
  per-token tracking.
  The comparison uses the token's `iat_ms` claim, not `iat`. JWT `iat` is a whole
  number of seconds, and registration stamps `passwordChangedAt` immediately
  before minting the first token, so comparing against the second-resolution
  `iat` cannot tell "issued just before the change" from "issued just after" -
  it signs out every new signup. Tokens without `iat_ms` (none in flight) fall
  back to a whole-second comparison.
- **Accounts predating `passwordChangedAt` still work.** A missing field means
  "the password has not been changed since", not "expired"; treating it as
  expired would lock out every pre-existing document.
- **Requests do not reveal whether an account exists.** `forgot-password`
  returns an identical `202` for unknown, suspended and real addresses, and the
  email body contains no account detail. Suspended accounts get no link at all.
- **Change-password requires the current password.** An access token proves the
  session is authenticated, not that the holder may rotate the credential.
  Reusing the current password is rejected so nobody believes they have rotated
  a compromised password when they have not.

> **A wrong current password is `400`, never `401`.** The token is fine; the
> user mistyped. This is load-bearing for the client, which reads any `401` as
> "this session is over" and drops the user to the login screen. Answering a
> typo with `401` signs people out for mistyping a password. The client
> additionally allowlists the four codes that really do end a session
> (`UNAUTHORIZED`, `INVALID_TOKEN`, `TOKEN_REVOKED`, `PASSWORD_CHANGED`), so
> `INVALID_CREDENTIALS` from a failed login never clears anything either.
- **Delivery is pluggable** (`app/services/email.py`): `console` logs the
  message, `smtp` sends it via `smtplib`. Delivery failure never changes the
  response - the TTL is the real expiry guarantee.
- **Audited** to `auditLogs` (DOC section 31): request, completion and change
  are all recorded, including requests for unknown addresses.

> **`devResetLink` is the field to watch.** It is populated only when
> `ENVIRONMENT` is not `production` and mail was not really delivered, so the
> flow is testable without a mailbox. If it is ever non-null in production,
> anyone can reset any account by asking for one. `CLIENT_BASE_URL` is
> server-side config for the same reason - never read it from a request body.

### Profile and photo (DOC sections 18, 21)

Four endpoints under `/api/v1/users` plus a public file route. Every route acts
on the caller's **own** account; there is deliberately no `/users/{id}` yet,
because letting an admin edit arbitrary users needs an RBAC story this phase
does not have.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/v1/users/me` | Bearer | read the stored profile |
| `PATCH` | `/api/v1/users/me` | Bearer | update `name` and `phone` |
| `POST` | `/api/v1/users/me/photo` | Bearer | upload or replace the photo |
| `DELETE` | `/api/v1/users/me/photo` | Bearer | remove the photo and its file |
| `GET` | `/api/v1/avatars/{file_name}` | none | serve the stored JPEG |

`GET /users/me` re-reads the document, so it reflects a change made in another
tab immediately - unlike the cheaper `/auth/me`, which is a session restore.

```powershell
$login = Invoke-RestMethod http://127.0.0.1:9999/api/v1/auth/login -Method Post `
  -ContentType 'application/json' -Body '{"email":"aarav@example.com","password":"parkeasy123"}'
$h = @{ Authorization = "Bearer $($login.accessToken)" }

Invoke-RestMethod http://127.0.0.1:9999/api/v1/users/me -Headers $h
Invoke-RestMethod http://127.0.0.1:9999/api/v1/users/me -Method Patch -Headers $h `
  -ContentType 'application/json' -Body '{"name":"Aarav Shah","phone":"+91 90000 00000"}'

# Clear the number rather than omit it: {} would leave it untouched.
Invoke-RestMethod http://127.0.0.1:9999/api/v1/users/me -Method Patch -Headers $h `
  -ContentType 'application/json' -Body '{"phone":null}'

Invoke-RestMethod http://127.0.0.1:9999/api/v1/users/me/photo -Method Post -Headers $h `
  -Form @{ file = Get-Item .\photo.jpg }
Invoke-RestMethod http://127.0.0.1:9999/api/v1/users/me/photo -Method Delete -Headers $h
```

**How it works**

- **`email` is not writable.** It is the login identifier and the DOC defines no
  change-of-address flow, so sending it is a `422` (`extra="forbid"`), not a
  silent no-op - a client bug surfaces instead of the user believing they
  re-keyed an address they had not. `role`, `status` and `facilityIds` are
  rejected the same way.
- **`PATCH` distinguishes "absent" from "empty".** Omitting `phone` leaves it
  alone; sending `null` or `""` clears it. `name` is non-nullable in the
  database, so `name: null` and a blank name are both `422`s. Whitespace inside
  a name collapses to single spaces, and only digits plus a leading `+` survive
  in `phone`.
- **Uploads are always re-encoded.** The server decodes the bytes with Pillow,
  rejects anything that is not really a JPEG/PNG/WebP *by inspecting the decoded
  data* (never the extension or the client's MIME type), honours EXIF
  orientation, flattens alpha onto white, downscales to the long edge and writes
  a progressive JPEG. `uploads/` therefore only ever contains small,
  server-generated files - there is no path by which an untrusted file lands on
  disk verbatim.
- **A replace deletes the old file**; `DELETE` clears the field first and then
  unlinks, because a stale file costs a few kilobytes while a `photoUrl` with no
  file behind it costs a broken image in the UI.
- **Errors carry a reason.** Photo failures are `400`s whose
  `error.details.reason` is one of `PHOTO_EMPTY`, `PHOTO_TOO_LARGE`,
  `PHOTO_UNREADABLE`, `PHOTO_FORMAT_UNSUPPORTED`, `PHOTO_TOO_MANY_PIXELS`, so
  the client can branch without parsing prose.
- **The avatar route is deliberately public.** An `<img src>` cannot attach an
  `Authorization` header, so a token-protected image URL would simply not render.
  The filename is the capability: 16 random bytes minted server-side, matched
  against an exact pattern *before* touching the filesystem, which is what keeps
  `../` unreachable rather than merely unlikely.
- **The body has a hard ceiling.** `MAX_REQUEST_BODY_BYTES` (4 MB) is refused
  before routing with a `413 REQUEST_TOO_LARGE`. It sits above `AVATAR_MAX_BYTES`
  (2 MB) on purpose, so an oversized photo gets the specific `400` and only a
  grossly oversized body gets the generic one.
- **Audited** as `PROFILE_UPDATED`, `PROFILE_PHOTO_CHANGED` and
  `PROFILE_PHOTO_REMOVED`. The log records field *names*, upload size and the
  client's declared filename - never the personal values or the image bytes.

### Vehicles (DOC sections 10, 18, 21)

Five endpoints under `/api/v1/vehicles` - exactly the operations DOC section 21
assigns to the group ("CRUD vehicles, default vehicle"). Every route acts on
the caller's **own** vehicles: `userId` comes from the token, never from the
body or the query string.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/v1/vehicles` | Bearer | list the caller's vehicles, oldest first |
| `POST` | `/api/v1/vehicles` | Bearer | register a vehicle (`201`) |
| `PATCH` | `/api/v1/vehicles/{id}` | Bearer | edit a vehicle |
| `DELETE` | `/api/v1/vehicles/{id}` | Bearer | remove a vehicle (`204`) |
| `PATCH` | `/api/v1/vehicles/{id}/default` | Bearer | make it the default vehicle |

```powershell
$login = Invoke-RestMethod http://127.0.0.1:9999/api/v1/auth/login -Method Post `
  -ContentType 'application/json' -Body '{"email":"aarav@example.com","password":"parkeasy123"}'
$h = @{ Authorization = "Bearer $($login.accessToken)" }

Invoke-RestMethod http://127.0.0.1:9999/api/v1/vehicles -Headers $h

# The first vehicle becomes the account's default automatically.
Invoke-RestMethod http://127.0.0.1:9999/api/v1/vehicles -Method Post -Headers $h `
  -ContentType 'application/json' `
  -Body '{"registrationNumber":"GJ 05 AB 1234","type":"sedan","model":"Honda Civic","fuelType":"petrol"}'

Invoke-RestMethod http://127.0.0.1:9999/api/v1/vehicles -Method Patch -Headers $h `
  -ContentType 'application/json' -Body '{"fuelType":"electric"}'

Invoke-RestMethod http://127.0.0.1:9999/api/v1/vehicles/<id>/default -Method Patch -Headers $h
Invoke-RestMethod http://127.0.0.1:9999/api/v1/vehicles/<id> -Method Delete -Headers $h
```

**How it works**

- **The fields are the ones DOC section 18 lists**: `registrationNumber`,
  `type`, `model`, `fuelType` - plus `isDefault`, which sections 10 and 21 both
  ask for. There is no `color` and no `make`; `model` is free text that carries
  both ("Honda Civic").
- **`isEV` is derived, never accepted.** It is `fuelType == "electric"`, so a
  stored vehicle can never claim `fuelType: "petrol"` and `isEV: true` at the
  same time. Sending `isEV` (or `userId`, or `isDefault` on the generic
  `PATCH`) is a `422` rather than a silently ignored field.
- **The registration number is normalised** to uppercase letters and digits
  before it is stored, so `ABC-1234`, `ABC 1234` and `abc1234` are one key. A
  repeat among the same account's vehicles is `409 REGISTRATION_DUPLICATE`; a
  *different* account may legitimately hold the same plate.
- **Exactly one default while any vehicle exists.** The first vehicle created
  becomes the default, `PATCH /{id}/default` switches it (clear the old flags,
  then set the new one), and deleting the default promotes the oldest remaining
  vehicle. A partial unique index makes two defaults impossible at the database
  level, and `GET` repairs a missing default, so an interrupted request can
  never leave an account without one.
- **Unknown ids and other users' ids are the same `404`**, with the same
  message, so the endpoints cannot be used to probe for foreign vehicle ids
  (DOC section 31).
- **Audited** as `VEHICLE_CREATED`, `VEHICLE_UPDATED`, `VEHICLE_DELETED` and
  `VEHICLE_DEFAULT_CHANGED`, recording field *names* only.
- **Writes are rate limited** per user (`VEHICLE_WRITE_RATE_LIMIT`); `GET` is
  not, because dashboards poll it.
- **A ceiling** of `VEHICLE_MAX_PER_USER` vehicles per account answers
  `409 VEHICLE_LIMIT_REACHED` instead of accepting more.

### How it works (general)

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
  working - each login gets its own `jti`. A password change is different: it
  invalidates every session at once.
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
| `FORGOT_PASSWORD_RATE_LIMIT` / `..._WINDOW_SECONDS` | `5` / `3600` | per IP+email; the anti-email-bombing limit |
| `RESET_PASSWORD_RATE_LIMIT` / `..._WINDOW_SECONDS` | `10` / `3600` | per IP+token |
| `CHANGE_PASSWORD_RATE_LIMIT` / `..._WINDOW_SECONDS` | `5` / `900` | per user |
| `PROFILE_UPDATE_RATE_LIMIT` / `..._WINDOW_SECONDS` | `20` / `3600` | per user |
| `AVATAR_UPLOAD_RATE_LIMIT` / `..._WINDOW_SECONDS` | `10` / `3600` | per user |
| `VEHICLE_WRITE_RATE_LIMIT` / `..._WINDOW_SECONDS` | `60` / `3600` | per user; create / edit / delete / set-default |
| `VEHICLE_MAX_PER_USER` | `20` | vehicles per account; the ceiling answers `409` |
| `AVATAR_MAX_BYTES` | `2097152` | 2 MB ceiling on the uploaded file, checked before decoding |
| `AVATAR_MAX_DIMENSION` / `AVATAR_JPEG_QUALITY` | `512` / `82` | what every upload is re-encoded to |
| `AVATAR_DIR` | `uploads/avatars` | where stored JPEGs live; relative to `Server/`, gitignored |
| `MAX_REQUEST_BODY_BYTES` | `4194304` | global body ceiling, answered with `413` |
| `PASSWORD_RESET_EXPIRES_MINUTES` | `30` | how long a link stays valid |
| `PASSWORD_RESET_TOKEN_BYTES` | `32` | entropy of the emailed token |
| `EMAIL_TRANSPORT` | `console` | `console` or `smtp` |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` | empty | required for `smtp`; incomplete config falls back to `console` with a warning |
| `SMTP_USE_TLS` / `SMTP_USE_SSL` | `true` / `false` | `SSL` is for port 465 |
| `CLIENT_BASE_URL` | `http://localhost:5173` | origin the emailed link points at; server-side config only |

> **Before deploying:** set `SELF_REGISTER_ROLES=driver`. With the default,
> anyone who can reach the API can create an `admin` account. The app logs a
> `SECURITY:` warning on every boot while a privileged role is self-registerable,
> and logs it at `CRITICAL` when `ENVIRONMENT=production`.

Rate limiting is an in-process counter (`app/core/rate_limit.py`), which is
fine for the single Uvicorn worker used here. Move it to Redis before running
more than one process, or the limit is per-process and trivially multiplied.
This matters most for `forgot-password`, which is the endpoint most likely to
be abused for email bombing.

### Not yet implemented

- Refresh tokens and session listing (DOC section 21 lists `refresh`).
- Editing another user's profile: there is no `/users/{id}`, so an admin can
  only edit their own account. Needs an RBAC story first.
- Reading *another* account's vehicle: `/vehicles` is scoped to the caller, so
  an admin reviewing a booking cannot look a vehicle up by id either. Same RBAC
  reason as `/users/{id}`.
- Profile `preferences` and `status` (DOC section 21) - deliberately deferred.
- Email verification of the registered address.
- Password-history rules (rejecting reuse of the last N passwords).
- Admin approval for operator onboarding. New accounts are `ACTIVE`
  immediately; flip the default in `app/services/auth.py` when that lands.
- WebSocket `/ws` still accepts unauthenticated connections.

## Layout

```
app/
  main.py        application factory, lifespan, CORS, WebSocket channel
  core/          config (pydantic-settings), security (PyJWT + bcrypt), errors,
                 logging, rate limiting, request body ceiling
  db/            PyMongo AsyncMongoClient lifecycle, index definitions
  api/           dependencies (auth, RBAC, facility scope) and v1 routers
  models/        domain enums (DOC section 17), user document shape
  schemas/       shared envelopes plus the auth and profile request models
  services/      business logic - one module per domain (DOC section 24)
  sockets/       WebSocket connection manager + DOC section 26 event names
  jobs/          background jobs: expiry, reminders, prediction refresh
  utils/         MongoDB <-> API conversion helpers
tests/
  conftest.py             offline app fixture; disposable-database fixtures
  test_health.py          routing, envelopes, configuration, enums
  test_auth.py            auth/RBAC dependencies
  test_password_reset.py  forgot / reset / change against a real MongoDB
  test_profile.py         profile read/update, photo upload, avatar serving
  test_vehicles.py        vehicle CRUD, default switch, index guarantees
```

The auth, password-reset, profile and vehicle tests need a MongoDB on
`MONGODB_URI`; they use a throwaway `parkeasy_test` database that is dropped on
teardown, and skip the database-dependent cases when none is reachable. The
profile tests also redirect `AVATAR_DIR` into a temporary directory, so a run
never writes into the repository's `uploads/`. The rest of the suite runs fully
offline.

```bash
python -m pytest                       # whole suite
python -m pytest tests/test_password_reset.py
python -m pytest -k "not test_password_reset"   # skip the ones needing Mongo
```

> **Coverage gap.** `tests/` used to be hidden by a bare `tests/` entry in
> `.gitignore`, which let commit `848f63b` delete 536 lines of tests.
> `test_password_reset.py` and the disposable-database fixtures were written to
> replace the lost `test_auth_flow.py`, which is not recoverable from git.
> `register` / `login` / `logout` / `me` are exercised transitively by the
> password tests, but they have no dedicated file of their own yet.

## Conventions

- **Collections** keep the DOC section 18 names: `parkingFacilities`, `parkingSessions`, ...
- **Status values** are stored in the exact DOC section 17 spelling (`AVAILABLE`, `RESERVED`, ...).
  The client's lowercase literals are mapped at the API boundary.
- **Ids** are `ObjectId`s on the server and 24-character hex strings over the
  wire, **except** where a natural key is better: `revokedTokens._id` is the
  token `jti` and `passwordResetTokens._id` is the SHA-256 of the reset token.
  Both are already unique, so an `ObjectId` plus an index would buy nothing.
  `app/utils/mongo.py` holds `to_object_id()` for parsing path params and
  `serialize()` for converting documents to JSON. Let MongoDB generate `_id` on
  insert rather than inventing one; `insert_one` populates it in place.
- **Errors** always use one envelope: `{"error": {"code", "message", "details"}}`.
- **Photos** live on disk under `AVATAR_DIR`, never in MongoDB: a document dump
  should not carry binary blobs, and `FileResponse` streams them better. The URL
  is `/api/v1/avatars/<generated name>`, public because an `<img src>` cannot
  authenticate, and re-created on every upload so it is safe to cache forever.
- **Auth** is a JWT access token sent as `Authorization: Bearer <token>`, plus
  the `revokedTokens` denylist for logout. No refresh token in Phase 1.
- **Session invalidation** has two mechanisms: `revokedTokens` for one explicit
  sign-out, and `users.passwordChangedAt` for bulk invalidation after a password
  change. Prefer the second for anything account-wide - it needs no per-token row.
- **Routers** stay thin; all rules live in `app/services/`. Do not put business
  logic in route handlers (DOC section 47).
- **No** `passlib` (incompatible with bcrypt 5.x) and **no** `motor`
  (deprecated - use `pymongo.AsyncMongoClient`).
- **Default vehicle** is a flag on the vehicle (`isDefault`), not a pointer on
  the user, because it is read with the vehicle list on every dashboard load.
  The "at most one per user" guarantee is a partial unique index
  (`uniq_user_default`), not a transaction: MongoDB standalone has none, and an
  index holds even if a route forgets to check.

## Deviations from the requirements document

| Section | Document says | Here | Reason |
| --- | --- | --- | --- |
| 24, 25 | Node.js + Express, `app.js` / `server.js` | Python FastAPI, `app/main.py` | project decision |
| 26 | Socket.IO | native FastAPI WebSockets, same event names | fewer dependencies |
| 25 | Redis for locks and cache | MongoDB atomic operations for now | no Redis on this machine; revisit in Phase 3 |
| 12 | four roles, role chosen at login | `role` is a field on the account; login takes email + password only | the role decides the dashboard, so it must be a server-side property rather than a picker on the login form |
| 12 | admin approves operator onboarding | all roles self-register and start `ACTIVE` | requested for a four-role demo; governed by `SELF_REGISTER_ROLES` |
| 24, 25 | Nodemailer or a transactional email provider | `smtplib` from the standard library, plus a `console` transport | no provider credentials exist yet; the interface is the same either way so swapping it later touches one factory function |
| 31 | secure HTTP headers | CORS + GZip only; no `helmet` equivalent | FastAPI has no built-in security-headers middleware; add it before deploying |
| 18, 21 | `users` lists `name`, `email`, `role`, `preferences`, `status` | profile is `name`, `phone`, a photo; **`email` is read-only** | a change of address has no flow in the DOC, so accepting one silently would mislead; `preferences` deferred |
| 18 | no profile photo anywhere in the DOC | `POST/DELETE /users/me/photo`, served from `/api/v1/avatars/{name}` | requested for this build; the file is always re-encoded, so `uploads/` holds only compressed JPEGs |
| 10, 18 | `vehicles` lists `registrationNumber, type, model, fuelType, isEV` | same fields plus `isDefault`; **`isEV` is derived from `fuelType`** rather than sent by the client | keeping both writable would let them contradict each other, and the DOC never says when a hybrid counts as an EV - one derivation rule answers it everywhere |
