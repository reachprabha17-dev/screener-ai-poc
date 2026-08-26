# Implementation Plan: Authentication and Authorization

Scope: replace the stubbed `X-Actor` header trust with real authentication, and
turn the one existing role check into a real authorization layer.

**No AD or SSO now.** Local accounts with passwords, held in the `users` table
this schema already has. §2 is the whole reason this document is longer than it
needs to be: it lists the five properties that let AD, LDAP or OIDC land later as
*one new adapter file plus a config value*, and nothing else changes.

Verified against the tree at `9c3d78f` (branch `migrate-postgres`).

---

## 0. What exists today, and the two holes

**The plumbing is already right, and it is the expensive half.** `Actor` is
threaded through every mutating service call (`service.py`), every mutating route
already takes `actor: Actor = Depends(get_actor)`, `users` already has
`roles_json`, `auth_ref` and `active`, and `auth_mode` is already a
`Literal["stub", "ldap", "local"]`. Nine call sites do not move.

What is missing is entirely inside `get_actor` (`api/deps.py:61`) and one table.

**Hole 1 — identity is whatever the client claims.** `X-Actor: anyone` is
accepted verbatim and written into the audit log as the person who approved a
rubric or signed off a run. `service._ensure_actor` then *creates* the `users`
row on first use, so a novel actor id is not even a lookup failure.

**Hole 2 — roles are self-granted.** `X-Actor-Roles: auditor` is honoured from
the browser. `auditor` is the only gate in the system (`schemas.py:388`,
`routes/audit.py:25`, `routes/candidates.py:139`, `routes/runs.py:124`) and it
gates the §15.2 field-exposure boundary — `sent_text`, `model_verdict`,
`match_ratio`. Any reviewer can open devtools and read the auditor view.

Both are documented and both are acceptable *only* under the loopback bind, which
is exactly why `deploy/screener-api.service` still says `--host 127.0.0.1`.

### 0.1 The staged nginx stopgap, and its one bug

`deploy/nginx-screener.conf` (staged, uncommitted) fronts the app with TLS and
`auth_basic`, strips client-supplied actor headers, and sets `X-Actor` from
`$remote_user`. As an interim measure that is sound and it closes Hole 1.

**It does not close Hole 2, and it breaks the auditor role outright.** nginx
treats `proxy_set_header X-Actor-Roles ""` as *omit this header*, not *send it
empty*. `deps.py:80` reads an absent header as the stub default,
`frozenset({"admin"})` — so every HR user behind the proxy is an admin, and no
request that arrives through the proxy can ever carry `auditor`. The audit log,
run stories and adverse-action records 403 for everybody, including the auditors
they exist for.

If the stopgap ships before §3–§8 land, the honest form of it is one line in
`deps.py` — read roles for the proxied actor out of `users.roles_json` rather
than out of a header — plus the startup refusal in §8.3. Otherwise keep the
config, and retire `auth_basic` when §5 lands.

---

## 1. The mechanism

**Session cookie, opaque handle, sessions table in Postgres.**

A `POST /auth/login` with username and password returns a `Set-Cookie` carrying
256 bits of `secrets.token_urlsafe(32)`. The server stores only
`sha256(token)`. Every subsequent request resolves that cookie to a row in
`sessions`, joins `users`, and builds the `Actor` from the database.

Why this and not the two alternatives:

**Not a JWT.** A signed token puts roles in the client's hands and makes
revocation a second mechanism you have to build anyway — offboarding, a
deactivated account, a role that was just narrowed. This system's whole premise
is that a decision affecting a person must be attributable and reversible; a
five-minute window where a fired recruiter still holds a valid token is the
opposite of that. A JWT also introduces a signing key, which this deployment
currently does not have (`plan_production_readiness.md` §3: *there are no
credentials in the codebase*), and it would have to be rotated. An opaque random
handle keeps that property: **no new secret material anywhere.**

**Not HTTP Basic (nginx or otherwise).** Basic auth cannot express roles, cannot
log out, cannot lock an account, replays the password on every single request,
and puts the credential check outside the process that writes the audit log — so
"who logged in" and "who approved this rubric" are records kept by two different
systems with no join between them.

The cookie is the right shape here for a reason specific to this app: the SPA is
served *by the API process, same-origin* (`app.py::_mount_reviewer_ui`,
`client.ts` — "same-origin, always"). There is no cross-origin case to solve, so
there is no reason to hand the browser a bearer token it has to store somewhere
XSS can reach.

### 1.1 Cookie attributes

| Attribute | Value | Why |
|---|---|---|
| `HttpOnly` | yes | JS cannot read it; an XSS in the bundle cannot exfiltrate the session |
| `Secure` | yes | TLS-only. Configurable off for `http://localhost` dev, and §8.3 refuses to start with it off in any non-stub mode |
| `SameSite` | **`Lax`** | see below |
| `Path` | `/` | the API and `/ui/` share an origin |
| `Domain` | *unset* | host-only cookie; no sibling host inherits it |
| name | `screener_session` | |

**`Lax` and not `Strict`, deliberately.** §15 and decision #11 both care that a
run is linkable — React Router exists here so "one screen per URL, so a run can
be linked to". Under `Strict` a colleague clicking a `/ui/runs/run-1/review` link
out of Teams or email arrives without the cookie and lands on a login screen
despite holding a live session, and the link they were sent is lost behind the
redirect. `Lax` withholds the cookie from cross-site *POST* — which is the CSRF
case — while sending it on top-level GET navigation, which is the link case.
The residual gap (cross-site GET that mutates) does not exist because no GET in
this API mutates.

### 1.2 Lifetime

* **Idle timeout — 30 minutes**, sliding. `last_seen_at` is bumped at most once
  per 60 s, so an active reviewer costs one extra write a minute rather than one
  per request.
* **Absolute lifetime — 12 hours**, not extendable. Caps the value of a stolen
  cookie regardless of activity.
* **Rotated on login.** A fresh row and a fresh token every time; the pre-login
  handle is never elevated. (Session fixation.)
* **Revoked immediately on:** logout, password change, role change, account
  deactivation, and admin-initiated "sign out everywhere". All of these are one
  `UPDATE sessions SET revoked_at = ... WHERE user_id = ?` and are the entire
  reason the sessions live in the database.
* **Pruned** by `screener auth prune-sessions` on a systemd timer (§9), plus a
  lazy delete when an expired row is read. Not by the worker — the worker screens
  resumes, and adding a second kind of sweep to a loop that already has lease
  reclaim is how the lease reclaim gets broken.

### 1.3 Passwords

argon2id via `argon2-cffi`, at the current OWASP floor: `m=19456` (19 MiB),
`t=2`, `p=1`. About 50 ms per verify on this hardware, which is only paid at
login, and §1.4's throttle stops it being used as an amplifier.

Policy, per **NIST SP 800-63B** and not the folklore:

* minimum 12 characters, maximum 128, all Unicode allowed, no truncation;
* **no composition rules** (no "must contain a symbol") and **no forced
  expiry** — both measurably push people toward `Summer2026!` and a sticky note;
* scored with **`zxcvbn`** and rejected below score 3, with the username, the
  display name, `screener` and the position references passed as context terms.
  Pure Python and fully offline, which matters because this host has no internet
  (decision #1) and HIBP's range API is not reachable. A static top-10k list was
  the first draft of this rule and is strictly worse: it passes `Screener2026!`
  and `alice-hiring`, which are the passwords this deployment will actually get;
* rehash on login when `argon2.PasswordHasher.check_needs_rehash` says the
  parameters have moved on. Free, and it means raising the cost later does not
  need a password reset.

### 1.4 Login throttling and account lockout

Two layers, both needed for different attacks:

* **Per account** — `users.failed_attempts` / `users.locked_until`. After 5
  consecutive failures, lock for `min(2^(n-5), 15)` minutes. Cleared on success.
  Stops a slow spray against one known account.
* **Per source IP** — an in-process fixed-window counter, 20 attempts / 5 min.
  The API is one process on one host (`_shared_service` is `lru_cache`'d per
  process), so this needs no Redis and should not acquire one. Stops one client
  walking a user list.

**Two properties that are easy to lose:**

1. When the username does not exist, still run a verify against a fixed dummy
   hash before returning. Otherwise response timing enumerates the user list.
2. The response is the same for wrong-user, wrong-password and locked:
   `401 {"detail": "Invalid username or password."}`. A distinct "account
   locked" message is an enumeration oracle *and* a denial-of-service primitive
   — an attacker can lock every account they can name and then read which ones
   locked. Locked accounts are visible to an admin (§7) and in the audit log,
   not to the person at the login form.

---

## 2. The AD / SSO door

Nothing below is built now. This section exists to name the five properties that
make it a day's work later instead of a rewrite, so that §3–§10 do not
accidentally violate them.

**The insight: everything downstream of "a session exists" is
provider-agnostic.** The cookie, the sessions table, `Actor`, the permission
check, every route, the whole audit trail — none of it depends on whether the
credential was checked by argon2, an LDAP bind, or an OIDC `id_token`. Only two
things differ: *how a credential is verified*, and *how a user row comes to
exist*. Both are already isolable.

### The five rules

1. **`users.id` is an internal, stable identifier — never the AD username, never
   a DN, never an email.** It is the foreign key in `positions.created_by`,
   `rubrics.approved_by`, `runs.reviewed_by`, `overrides.actor_id` and
   `audit_log.actor_id`. If it were the AD login, then a rename, a domain
   migration or a marriage rewrites history — or worse, silently doesn't. §3 adds
   `username` (how you log in) and `auth_ref` (the external subject) as separate
   columns so that **migrating a user to AD is an UPDATE of `auth_ref`, and their
   entire audit history follows them.**
2. **Roles live in the local `users` table, not in the cookie and not solely in
   the IdP.** An AD group can *map* to a role; it does not *become* one. This
   keeps role changes revocable and auditable here, keeps a break-glass local
   admin possible when the directory is unreachable, and means §6's permission
   table is the single answer to "who may do what" under every provider.
3. **The cookie is an opaque handle, not a claims container.** Switching IdPs
   never changes the cookie format, the session table, or a single line of the
   authorization layer.
4. **Credential verification sits behind a Protocol in `ports.py`**, per §6 of
   the spec — the same mechanism that makes Ollama swappable for vLLM. `service.py`
   never imports `argon2`.
5. **Every route depends on an `Actor` and a `Permission`.** No route ever asks
   *how* the actor authenticated.

### What each provider then costs

| Provider | New code | Config |
|---|---|---|
| **LDAP / AD** | `clients/ldap_identity.py` — bind as the user, search for groups. One file satisfying `IdentityProvider`. | `auth_mode=ldap`, server URL, base DN, bind DN + password (**the first real secret; `EnvironmentFile=` at 0600, per `plan_production_readiness.md` §3**), `group_role_map` |
| **OIDC / Entra ID** | `clients/oidc_identity.py` + `api/routes/auth_oidc.py` — the redirect flow is a different *shape* from username/password, so it gets two routes (`/auth/oidc/start`, `/auth/oidc/callback`) rather than being forced into `authenticate()`. Both end in the same `service.begin_session(user_id)`. | `auth_mode=oidc`, issuer, client id + secret, `group_role_map` |

**Provisioning under an external provider:** just-in-time create on first
successful login, with **zero roles**. Deny by default — a directory account is
not an entitlement — and an admin grants roles, or `group_role_map` does it from
the returned groups. Existing local users are linked by writing their
`auth_ref`; no row is recreated and no audit history is orphaned.

**Mixed mode is supported by construction.** `users.auth_provider` is per-row, so
one local break-glass admin can coexist with a directory full of AD users. That
is not a nice-to-have: it is what stops a directory outage locking everyone out
of a system holding hiring decisions.

**The same door fits an on-prem IdP.** Keycloak or Authentik brokering AD is an
OIDC provider like any other, so the row above is unchanged if that is the route
taken. §15 weighs it as an alternative to building §1 at all.

---

## 3. Schema — migration `0002.authentication.sql`

Follows the existing conventions in `0001.initial-schema.sql`: TEXT ISO-8601
timestamps (documented there as deliberate), `?` placeholders, a matching
`.rollback.sql`.

```sql
ALTER TABLE users ADD COLUMN username TEXT;
ALTER TABLE users ADD COLUMN auth_provider TEXT NOT NULL DEFAULT 'local';
ALTER TABLE users ADD COLUMN password_hash TEXT;
ALTER TABLE users ADD COLUMN password_changed_at TEXT;
ALTER TABLE users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN failed_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN locked_until TEXT;

UPDATE users SET username = id WHERE username IS NULL;   -- 'poc-operator' etc.
CREATE UNIQUE INDEX idx_users_username ON users (LOWER(username));
CREATE UNIQUE INDEX idx_users_auth_ref ON users (auth_provider, auth_ref)
  WHERE auth_ref IS NOT NULL;

CREATE TABLE sessions (
  id            TEXT PRIMARY KEY,
  user_id       TEXT NOT NULL REFERENCES users(id),
  token_sha256  TEXT NOT NULL UNIQUE,
  created_at    TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  expires_at    TEXT NOT NULL,       -- absolute; never extended
  revoked_at    TEXT,
  client_ip     TEXT,
  user_agent    TEXT
);
CREATE INDEX idx_sessions_user ON sessions (user_id) WHERE revoked_at IS NULL;
CREATE INDEX idx_sessions_expiry ON sessions (expires_at);
```

Notes on choices that are not obvious:

* **`username` is separate from `id`.** Rule 1 of §2. The `LOWER()` unique index
  is what stops `Alice` and `alice` becoming two people with two audit trails.
* **`auth_ref` is kept, not repurposed.** `0001`'s comment offers it for *"LDAP DN
  or argon2 hash"*. Splitting those apart is the change: `auth_ref` is the
  external subject, `password_hash` is the secret. One polymorphic column holding
  either would make "is this user federated?" a string-shape guess, and would put
  a password hash in a column an LDAP migration wants to overwrite.
* **`token_sha256`, not the token, and not argon2 over it.** The token is 256
  bits of CSPRNG output, so it has no guessable structure to slow down — SHA-256
  is the correct tool and a per-request argon2 verify would be absurd. Hashing at
  rest means a database read (backup, replica, `pg_dump` in a ticket) does not
  hand over live sessions.
* **No `password_expires_at`.** §1.3.
* **`sessions.user_id` has no `ON DELETE CASCADE`** because nothing deletes users
  — deactivation is `active = FALSE`, so the audit foreign keys stay intact. This
  matches how `purge_candidate` handles erasure elsewhere.

---

## 4. Ports and adapters

**`screener/ports.py`** (imports only `models`, per `test_ports_imports_only_models`):

```python
class Principal(BaseModel):
    """Who the provider says this is. Not yet an Actor — no roles here."""

    model_config = ConfigDict(frozen=True)
    subject: str  # stable external id: objectGUID, OIDC `sub`, or username locally
    username: str
    display_name: str = ""
    groups: tuple[str, ...] = ()


@runtime_checkable
class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...
    def verify(self, hashed: str, password: str) -> bool: ...
    def needs_rehash(self, hashed: str) -> bool: ...


@runtime_checkable
class IdentityProvider(Protocol):
    name: str

    def authenticate(self, username: str, password: str) -> Principal | None: ...
```

**`screener/clients/argon2_hasher.py`** — the only module that imports `argon2`.
`verify` returns `False` on `VerifyMismatchError` rather than raising, so the
service has one code path.

**`screener/clients/local_identity.py`** — `IdentityProvider` over `users`. This
is the only adapter built now; `ldap_identity.py` is the door in §2.

Add `argon2-cffi` to `[project].dependencies` in `pyproject.toml`.

`tests/test_layering.py::test_each_store_provides_what_its_protocol_declares`
already walks Protocols against implementations — the new pair is picked up by
extending that parametrisation.

---

## 5. Authorization — `screener/core/authorize.py` (pure)

Authorization is a decision that can stop a person from being interviewed. Per
Rule 1 of the codebase it is a plain function with no side effects, tested in
milliseconds, and it lives in `core/`.

```python
class Permission(StrEnum):
    POSITION_WRITE = "position:write"
    RUBRIC_WRITE = "rubric:write"
    RUBRIC_APPROVE = "rubric:approve"
    RUN_START = "run:start"
    DECISION_RECORD = "decision:record"
    RUN_SIGN_OFF = "run:sign_off"
    CANDIDATE_PURGE = "candidate:purge"
    AUDIT_READ = "audit:read"
    USER_ADMIN = "user:admin"


ROLE_PERMISSIONS: Mapping[str, frozenset[Permission]] = {...}


def permitted(roles: frozenset[str], permission: Permission) -> bool:
    return any(permission in ROLE_PERMISSIONS.get(r, frozenset()) for r in roles)
```

Roles are the four §5 already names: `recruiter`, `hiring_manager`, `auditor`,
`admin`.

| | recruiter | hiring_manager | auditor | admin |
|---|---|---|---|---|
| position / rubric write | ✅ | ✅ | | |
| rubric **approve** | | ✅ | | |
| run start / rescan / abort | ✅ | ✅ | | |
| record decision | ✅ | ✅ | | |
| run **sign-off** | | ✅ | | |
| candidate purge | | | | ✅ |
| audit read (**and §15.2 auditor fields**) | | | ✅ | |
| user admin | | | | ✅ |

**`admin` does not imply `auditor`, and this is the point.** The whole reason
`RunStory.separation_of_duties` is computed (`models.py:156`) is that the person
who ran the screening must not be the person who certifies it was run properly.
An `admin` role that silently absorbs `auditor` makes that field a decoration.
The existing `SessionProvider` already refuses to grant `auditor` by default for
exactly this reason (`web/src/session/SessionProvider.tsx:8-16`) — keep the
argument, move the enforcement server-side.

**The honest limit:** an `admin` holds `USER_ADMIN` and can therefore grant
themselves `auditor`. On a single host where the same people have shell and
Postgres access, that cannot be prevented — so it is made *detectable* instead:
role changes write an audit row (§10), and `GET /audit` is the screen where that
shows up. Say this out loud in the deployment notes rather than implying a
control that isn't there.

`GET /dashboard` stays ungated — `routes/dashboard.py:8` argues it names no
candidate, and that property should be re-asserted in the test, not removed.

---

## 6. Service layer — `screener/service.py`

`api/` may not import `screener.storage` **at all**
(`test_no_api_module_imports_storage_at_all`, which explicitly refuses
exceptions). So every auth operation is a service method, and `deps.py` calls it.

New stores: `storage/users_store.py` (grown out of the three functions currently
squatting in `positions_store.py`) and `storage/sessions_store.py`.

New service surface:

```python
def authenticate(self, username, password, *, client_ip, user_agent) -> str
def resolve_session(self, token: str) -> Actor | None
def end_session(self, token: str) -> None
def end_all_sessions(self, user_id: str, actor: Actor) -> int
def change_password(self, user_id, current, new, actor: Actor) -> None
def create_user(self, *, username, display_name, roles, actor: Actor) -> str
def set_roles(self, user_id, roles, actor: Actor) -> None
def set_active(self, user_id, active: bool, actor: Actor) -> None
def list_users(self) -> list[UserSummary]
```

`authenticate` is one transaction: throttle check → provider `authenticate()` →
reset or increment `failed_attempts` → insert session → audit row. One
transaction is what makes the failure counter honest under concurrent attempts,
and this codebase already owns that pattern (`uow.py`, §12.4).

New errors, mapped once in `app.py::_install_error_handlers` alongside the
existing four — so routes stay at the four lines §15.4 asks for:

* `AuthenticationError` → **401** (+ `WWW-Authenticate` omitted deliberately; a
  browser-native basic-auth prompt over a JSON API is a worse experience than the
  SPA's own login screen)
* `AuthorizationError` → **403**

**`_ensure_actor` (`service.py:191`) becomes `_require_active_user`** — a lookup
that raises `AuthenticationError` when the user is absent or inactive, instead of
`positions_store.seed_user` inserting whatever it was handed. Its own docstring
already predicts this: *"When LDAP or local auth lands this becomes a lookup
against the real directory rather than an insert, and no call site changes."*
That is the whole retrofit, and it is one function.

`seed_user` survives for exactly one caller: the CLI bootstrap in §9.

---

## 7. API layer

### 7.1 `deps.py` — the seam, filled in

```python
def get_actor(request: Request, service: ScreenerService = Depends(get_service)) -> Actor:
    if settings.auth_mode == "stub":
        ...  # unchanged; §8.3 keeps it on loopback
    token = request.cookies.get(SESSION_COOKIE)
    actor = service.resolve_session(token) if token else None
    if actor is None:
        raise HTTPException(401, "Not signed in.")
    return actor


def require(permission: Permission) -> Callable[[Actor], Actor]:
    """Dependency factory. `actor: Actor = Depends(require(Permission.AUDIT_READ))`."""
```

`X-Actor` and `X-Actor-Roles` stop being read outside `auth_mode == "stub"`.

### 7.2 Routes — `screener/api/routes/auth.py`

| Route | Auth | Notes |
|---|---|---|
| `POST /auth/login` | none | body `{username, password}`; sets the cookie; returns the identity |
| `POST /auth/logout` | session | revokes, clears the cookie; **200 even if already logged out** |
| `GET  /auth/me` | session | `{id, username, display_name, roles, permissions}` — the SPA's source of truth |
| `POST /auth/password` | session | self-service change; revokes every other session |
| `GET/POST /users…` | `USER_ADMIN` | create, set roles, activate/deactivate, reset password, sign-out-everywhere |

**Unauthenticated by design:** `/health`, `/ready` (systemd and the proxy consume
these; the nginx config should not expose them beyond the VPN), `POST /auth/login`,
and the `/ui/` bundle — the login screen has to be servable to be usable. Nothing
else.

The four existing hand-rolled gates
(`routes/audit.py:25`, `routes/candidates.py:139`, `routes/runs.py:124`,
`schemas.py:388`) become `Depends(require(Permission.AUDIT_READ))`. **`schemas.py:388`
stays as a role check**, not a permission one — it is the §15.2 field-exposure
boundary and it belongs next to the view types, exactly where its docstring says.

### 7.3 CSRF — one middleware

Cookie auth needs it. `SameSite=Lax` covers cross-site form POSTs; the belt to
its braces is an origin check in `app.py`, next to the existing `_no_store`
middleware:

> For any request whose method is not GET/HEAD/OPTIONS: require `Origin` (or
> `Sec-Fetch-Site: same-origin`) to match the request's own host. Reject with
> **403** otherwise.

Roughly ten lines, no token plumbing, no per-form state, and it works for the SPA
because every mutating call already goes through `client.ts` on the same origin.
A double-submit token becomes necessary only if this app ever gains a
cross-origin caller — which decision #11 exists to prevent.

### 7.4 Trusting `X-Forwarded-For`

The per-IP throttle and `sessions.client_ip` are worthless if the client can set
the header. Add `trusted_proxies: list[str]` to settings; parse
`X-Forwarded-For` **only** when the immediate peer is in that list, and take the
rightmost untrusted hop. Default empty, which means "use the socket peer" — the
correct behaviour on loopback.

---

## 8. Configuration and startup

### 8.1 `config/settings.py`

```python
auth_mode: Literal["stub", "local", "ldap", "oidc"] = "stub"  # + oidc
session_idle_minutes: int = 30
session_absolute_hours: int = 12
session_cookie_secure: bool = True
login_max_failures: int = 5
login_lockout_minutes: int = 15
password_min_length: int = 12
trusted_proxies: list[str] = []
```

`dev_actor_id` stays, used only by `auth_mode == "stub"` and the CLI default.

### 8.2 No new secrets

Under `auth_mode="local"` there is still nothing to manage — no signing key, no
bind password, no client secret. `plan_production_readiness.md` §3's "no
credentials in the codebase" survives this plan intact, and stops surviving it at
`auth_mode=ldap`. That is a real reason to prefer local accounts until AD is
actually required.

### 8.3 Startup gates in `create_app`

This is item 4 of `plan_production_readiness.md`'s "do now" list, which currently
exists only as a comment. Refuse to construct when:

* `auth_mode == "stub"` and `api_host` is not a loopback address — *"comments do
  not survive a deployment change"*;
* `auth_mode != "stub"` and `session_cookie_secure` is `False`;
* `auth_mode == "local"` and no active user holds `USER_ADMIN` — otherwise the
  first deployment is a locked door.

Same pattern as `require_current_schema()`: fail at startup, in the journal,
where an operator reads it — not at the first request.

---

## 9. CLI and bootstrap — `screener/cli.py`

The CLI is break-glass and runs as an OS user on the host who already holds the
Postgres credentials. **Shell access *is* its authentication**, and pretending
otherwise by prompting for a password there would be theatre. What changes is
that `--actor` must now name an **existing, active** user rather than
conjuring one.

```
screener users create   --username alice --name "Alice Chen" --role recruiter
screener users passwd   --username alice          # prompts twice, hidden
screener users roles    --username alice --role recruiter --role auditor
screener users disable  --username alice          # + revokes every session
screener users list
screener auth prune-sessions                      # systemd timer, daily
```

`users create` prints no password and generates none: an admin sets one
interactively, or `--must-change` marks the account so the first login forces a
change. A default or emailed password is the single most common way an internal
tool ends up with `Welcome123` on ten accounts.

**Bootstrap:** `screener users create --username admin --role admin` then
`users passwd`, run once on the host, before flipping `auth_mode` to `local`.
§8.3's third gate is what makes forgetting this loud instead of silent.

---

## 10. Audit and observability

Auth events go in the existing `audit_log` — the same table an auditor already
reads (`search_audit`, `run_story`), so a login and a rubric approval sit on one
timeline. New actions: `auth.login`, `auth.login.failed`, `auth.logout`,
`auth.locked`, `auth.password.changed`, `auth.session.revoked`, `user.created`,
`user.roles.changed`, `user.deactivated`.

**One constraint to design around:** `audit_log.actor_id` is `NOT NULL REFERENCES
users(id)` (`0001.initial-schema.sql:190`), so a failed login against a
*nonexistent* username has no row to point at. Do not relax the foreign key for
it. Failed logins for a **known** user get an audit row; failures for an unknown
username go to structlog only, with the attempted username recorded at
`warning`. The audit log stays a record of what *people in this system* did,
which is the question it exists to answer.

`logging.py` already threads `actor_id`; add `session_id` to the same bound
context so a session can be followed across requests. **Never log the token, the
password, or the hash** — add an assertion for this to
`tests/test_logging_failures.py`, which already exists to catch this class of
leak.

---

## 11. Reviewer interface — `web/src/`

`SessionProvider` currently *is* the identity: it holds a name and a role list in
`localStorage` and puts them on every request
(`web/src/api/client.ts::headers`). It becomes a consumer instead.

* **`GET /auth/me` via TanStack Query is the only source of identity.** Roles are
  read from the server and are display-only. The UI hides what the server would
  403 anyway — defence in depth, never the control.
* **`client.ts`**: drop the `X-Actor` / `X-Actor-Roles` headers, add
  `credentials: 'same-origin'` explicitly. **A 401 from any call redirects to
  `/ui/login`, handled in the one place every request already funnels through** —
  `test_every_network_call_goes_through_the_one_client` guarantees there is no
  second path to patch.
* **New `LoginPage`** with a generic error (§1.4), plus a
  `ChangePasswordPage` that the `must_change_password` flag forces through.
* **Deep links survive login**: capture the attempted path, restore it after.
  This is the `SameSite=Lax` argument again from the other side — the linkable-run
  property is not worth losing to an auth flow.
* **`admin` users get a Users screen** (list, create, roles, disable, reset).
* **Remove `screener.actor` / `screener.roles` from `localStorage`**, and clear
  any stale values on first load — they are inert once the headers are gone, but
  leaving a role list in browser storage invites the next person to trust it.
* Logout button in the header, showing the signed-in display name.

---

## 12. Tests

Matching where this codebase already puts its rules:

**`tests/test_authorize.py`** (pure, fast) — the §5 permission table exhaustively:
every role × every permission, and specifically that `admin` does **not** carry
`AUDIT_READ` and `recruiter` does not carry `RUBRIC_APPROVE`.

**`tests/test_auth.py`** — login success and failure; lockout at the threshold
and its expiry; identical response for unknown-user / wrong-password / locked;
a dummy verify runs for an unknown user; idle expiry; absolute expiry that
activity cannot extend; token rotation on login; revocation on password change,
role change and deactivation; `check_needs_rehash` upgrade path; the literal
cookie attributes (`HttpOnly`, `Secure`, `SameSite=Lax`, no `Domain`).

**`tests/test_api.py`** — parametrised over **every** route: unauthenticated →
401 for all but the four in §7.2; a `recruiter` session → 403 on every auditor
route; a cross-origin `POST` → 403; and the §15.2 assertion the file already
makes, now driven by a real session rather than a header.

**`tests/test_layering.py`** — three additions:
`screener/api` imports neither `argon2` nor `screener.storage` (the latter is
already asserted, and auth is the most likely thing to try to break it);
no module under `api/routes` reads `X-Actor`; `core/authorize.py` is covered by
the existing `test_core_does_no_io`.

**`web/src/api/client.test.ts`** — no actor headers are sent; a 401 redirects.

The `bash scripts/dev.sh check` gate (ruff, mypy, pytest at 85% coverage) is the
bar, unchanged.

---

## 13. Order of work

Each phase leaves the tree deployable.

| Phase | Work | Size |
|---|---|---|
| **0 — today** | §8.3's stub-off-loopback gate; fix or retire the nginx role bug in §0.1. Nothing else changes. | ~1 h |
| **1 — foundation** | Migration 0002, `ports` + argon2 adapter, `core/authorize.py`, `users_store`/`sessions_store`, service methods, CLI `users` commands. Two new dependencies (`argon2-cffi`, `zxcvbn`) and the wheelhouse transfer that implies (§15). No route changes yet; `auth_mode` still `stub`. Fully testable behind the CLI. | 2–3 d |
| **2 — the switch** | `deps.py`, `auth.py` routes, error handlers, CSRF middleware, replace the four hand-rolled gates, login/logout/change-password/Users screens. Flip `auth_mode=local`, retire `auth_basic`, bind nginx→loopback as now. | 3–4 d |
| **3 — hardening** | Throttle and lockout, `zxcvbn` strength gate, `X-Forwarded-For` trust, prune timer, audit actions, admin sign-out-everywhere. | 1–2 d |
| **4 — the door** *(only when asked)* | `clients/ldap_identity.py`, `group_role_map`, JIT provisioning, linking existing users by `auth_ref`. Nothing from phases 1–3 is rewritten. | 1–2 d |

Phase 2 is the only one with a flag day, and it is one setting.

---

## 14. Deliberately not doing

* **MFA.** VPN-only, internal, small team. `users` does not preclude a TOTP
  column later, and nothing in §1 would change to add one.
* **Self-service password reset.** It needs email, and this host has no internet
  (decision #1). An admin resets via §9, which is also an audited event with a
  human in it.
* **"Remember me" / long-lived cookies.** Directly opposed to §1.2's absolute
  cap on the value of a stolen cookie.
* **API keys or service accounts.** The worker shares the database, not the HTTP
  API (`worker.py` → `service.py`), so it has no HTTP surface to authenticate.
  Adding keys now would create a credential class with no consumer.
* **Row-level authorization** (a recruiter seeing only their own positions).
  `positions.created_by` exists and `screener_spec_v6.md:1866` names this as the
  planned seam — it is a filter in the service layer, and it is a separate piece
  of work from *authentication*. Nothing here forecloses it.
* **A secrets vault.** §8.2 — there is still nothing to put in one.

---

## 15. Appendix — libraries considered

Every dependency here is a manual wheelhouse transfer onto an air-gapped host
(`screener_spec_v6.md:179`: `uv pip download` on a connected machine,
`--no-index --find-links` on the target), platform-matched to the deploy box.
That is not a reason to avoid libraries. It is a reason not to take a large tree
for a small problem.

Nothing auth-shaped is installed today: no `passlib`, `argon2`, `bcrypt`,
`cryptography`, `authlib` or `itsdangerous` among the 136 packages in the venv.
Everything below is a new dependency.

### Adopted

| Library | For | Why this one |
|---|---|---|
| **`argon2-cffi`** | password hashing (§1.3) | The reference argon2 binding, actively maintained. `check_needs_rehash` is what makes §1.3's cost-upgrade path free rather than a mass password reset. One compiled extension — download the wheel for the **target** architecture, not the dev box's. |
| **`zxcvbn`** | password strength (§1.3) | Pure Python, no data download, no network. Accepts context terms, so it rejects the passwords this specific deployment will get rather than the ones a generic list knows about. |

### Deferred to phase 4

| Library | For | Note |
|---|---|---|
| **`Authlib`** | OIDC | Sync-capable; handles discovery, PKCE and JWKS rotation correctly. **Do not hand-roll an OIDC client** — this is the one place in the plan where the library is unambiguously safer than the code. |
| **`ldap3`** or **`python-ldap`** | AD bind | `ldap3` is pure Python and transfers cleanly, but has been quiet since 2.9.1. `python-ldap` is better maintained and needs OpenLDAP headers on the host. Decide at phase 4 against the directory that actually exists, not now. |
| **`pyotp`** | TOTP, if MFA ever lands | Small and stdlib-shaped. §14. |

### Rejected

**`fastapi-users`** — the batteries-included answer, and the wrong fit. It is
async-only and requires the SQLAlchemy **async ORM** (or Beanie / Tortoise). This
codebase is sync `def` handlers by explicit decision — `api/app.py`'s module
docstring argues it — over SQLAlchemy **Core** with hand-written SQL and no ORM
(§22.2). Adopting it means async-ifying the API or forking its database adapter,
and it wants to own the `User` model that five foreign keys already point at. It
would be fought in precisely the places this repo has already reasoned carefully.

**`passlib`** — the reflex choice. Unreleased since 1.7.4 (2020), broken against
bcrypt 4.x, and it imports `crypt`, which PEP 594 removed in 3.13. Python 3.12
here, so that is a wall ahead rather than a wall now — but it is a wall on the
path. `pwdlib` is the maintained successor and a reasonable choice; it is
indirection over a single call while there is only one backend, so §4's
`PasswordHasher` Protocol already provides the seam it would provide.

**`starlette.middleware.sessions`** — present on disk and inert (`itsdangerous`
is not installed). It is a *signed client-side* cookie: the JWT-shaped thing §1
rejected, with no revocation and a signing key to hold and rotate.

**`slowapi` / `limits`** — the per-IP window in §1.4 is about fifteen lines in one
process. The dependency is larger than the code it replaces.

**No library for the session layer itself.** `secrets.token_urlsafe`,
`hashlib.sha256`, `hmac.compare_digest` and one table are the entire mechanism —
roughly forty lines. Each of §1.2's revocation rules is a decision that should be
readable in this repo rather than configured inside someone else's.

### The alternative architecture — an on-prem IdP

If AD is near-certain rather than hypothetical, **Keycloak or Authentik with this
app as an OIDC client** is the honest alternative to building §1 at all. AD
federation becomes IdP configuration rather than code, and MFA, password policy,
lockout, session management and an admin UI all arrive built and maintained.

What it costs on **this** box: a second service with its own database, on a host
already running Ollama, Postgres, the API and the GPU worker, installed through
the same air-gapped wheelhouse. It ends §8.2's no-secrets property. And it splits
the record — `audit_log` exists to answer *"prove this hiring decision was made
properly"*, and "who logged in" living in another product's tables makes that
answer a join across two systems, which is a real cost in the one area this
application is actually about.

**§2's door fits it either way.** An OIDC adapter points at Keycloak or at Entra
ID identically, so choosing this later is phase 4 with a different config value —
not a different plan. That is the property worth protecting, and it is protected
whichever way this question is answered.
