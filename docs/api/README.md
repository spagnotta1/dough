# The Dough API

`/api/v1` is the stable contract between this application and every client of
it — the web UI today, a native client tomorrow.

`openapi.yaml` beside this file is the formal specification and is the reference
for individual endpoints. This document is the part a specification is bad at:
the conventions that apply everywhere, the reasoning behind them, and what to do
about the endpoints that predate the version.

---

## Getting a credential

```
POST /api/v1/auth/login
{"username": "sal", "password": "…", "device_name": "iPhone"}

201
{"success": true,
 "data": {"token": "dgh_2mRWa1cDaxtM…", "token_type": "Bearer",
          "shown_once": true, "expires_at": "2026-10-28T…Z"},
 "meta": {…}}
```

Then, on every subsequent request:

```
Authorization: Bearer dgh_2mRWa1cDaxtM…
```

**The token is shown once.** Only its SHA-256 is stored, so it cannot be
recovered or resent. If a client loses it, revoke and issue another — which is
the same property that makes storing only the hash worth doing, and the same one
`HouseholdInvite` has.

Never put a token in a query string. The API does not read one, deliberately:
query strings are written to access logs by every proxy in the path, kept in
browser history, and sent in `Referer`. There is no way to use one safely, so
there is no code that would accept one.

### Scopes

| Scope | Permits |
|---|---|
| `read` | Safe methods (`GET`, `HEAD`, `OPTIONS`) |
| `write` | Everything. Implies `read`. |

A token without `write` receives `403` with `error.code = "insufficient_scope"`
and the required scope in `error.details` on any unsafe method. The fix is
mechanical — reissue with the scope — which is why it is a distinct code from
`forbidden`, where the fix is to ask a human.

Scopes narrow what a *token* may do below what its user may do. They never widen
it: role checks resolve through the user on every request, so a demoted owner's
existing tokens lose owner powers immediately, and a removed member's tokens
stop working.

### Revoking

```
GET    /api/v1/auth/tokens        list, including revoked ones
DELETE /api/v1/auth/tokens/{id}   revoke
```

A household owner may revoke any of the household's tokens. Any token may revoke
*itself* with no owner present — a device must be able to sign itself out.

Revoked tokens stay in the list rather than being deleted. "Was that phone's
token ever revoked?" is exactly the question somebody asks after losing a phone,
and it has to be answerable from the product.

### What else stops a token working

A token is not only valid until it expires or is explicitly revoked. Four other
things end it, and a client should treat all of them as "obtain a new
credential" rather than as errors to retry:

| What happened | The token's `state` |
| --- | --- |
| An owner revoked it, or it revoked itself | `revoked` |
| It passed its `expires_at` | `expired` |
| **The account's password changed** | `stale` |
| Its user was removed from the household | `stale` |

The third is worth stating because nothing about the token itself changes. Every
credential is stamped with a generation counter (`AppUser.session_version`) when
it is created, and changing a password raises it — invalidating every token *and*
every browser session for that account at once. Nothing sweeps the token table
when that happens, so `revoked_at` stays null; `state` is what says so.

None of these are distinguishable in the response. All four answer `401` with
`error.code: "unauthenticated"` and the same message, for the reason a revoked
token and an unknown one answer identically: telling a caller its credential
*used to* work confirms it once held a real one. The distinction is in
`GET /api/v1/auth/tokens` — where the caller is already authenticated — and in
the audit log.

A client that stores a token should therefore treat a `401` as "log in again",
not as a transient failure, and must not retry the same credential.

### Session authentication

The web UI keeps using its signed session cookie, and `/api/v1` accepts it.
Unsafe methods from a session **still require `X-CSRF-Token`**.

That is not an inconsistency. CSRF exists because a browser attaches cookies to
cross-site requests *automatically* — the credential travels without the
attacking page knowing it. An `Authorization` header is never attached
automatically. So the CSRF requirement is waived for the **credential**, never
for the path: exempting `/api/v1/*` wholesale would reopen the hole for exactly
the calls the web UI makes.

---

## The envelope

Every response is one of two shapes.

```json
{"success": true,  "data": …, "meta": {…}}
{"success": false, "error": {"code": …, "message": …, "details": …}, "meta": {…}}
```

`meta` always carries `api_version` and `timestamp`, and carries `request_id`
whenever there is a request. That id is the same value returned in
`X-Request-ID` and written on every server log line for the request — so a
person quoting it off a screen resolves it to the exact lines. Surface it in
error UI.

Two documented exceptions, and only two:

- **`204 No Content`** has no body. A body containing `{"success": true}` would
  be a 200 wearing the wrong status code.
- **Streaming endpoints** answer `text/event-stream`. See below.

### `data` is the resource

For a collection, `data` is the array itself — never a container. Pagination
lives in `meta.pagination`, so a client never has to unwrap `data` differently
depending on whether an endpoint happened to paginate.

---

## Pagination

```
GET /api/v1/transactions?page=1&page_size=50&sort=date&order=desc
```

```json
"meta": {"pagination": {"page": 1, "page_size": 50, "total": 412,
                        "total_pages": 9, "has_next": true, "has_prev": false}}
```

- `page` is **1-based**.
- `page_size` is **clamped** to the server maximum (`/api/v1/settings` reports
  it), not refused. A client asking for 5,000 rows has made a judgement about
  its own memory, not an error.
- A **malformed** value *is* refused with `422`. `page=abc` is a client bug, and
  silently serving page 1 turns it into "the API keeps returning the first
  page" — reported as a server problem and investigated in the wrong place.
- `has_next`/`has_prev` are computed server-side so every client does not
  reimplement `page * page_size < total`, whose off-by-one is the reason a last
  page silently repeats on some clients and not others.

**Sorting is stable.** Every ordering carries the primary key as a tiebreak.
Without it, ten transactions on the same date have no defined order, so paging
can duplicate a row on one page and drop it from the next — nothing fails, the
client just shows the wrong ledger.

`sort` accepts only the fields the endpoint documents; anything else is `422`.
It reaches a SQL `ORDER BY`, so it is an allow-list rather than a sanitizer.

### Unpaged collections

`/accounts` and `/budgets` return everything. A household has a handful of each,
and paging them would make every client write a loop to reassemble something
that always fits in one response. These are the stated exceptions — every other
collection pages.

---

## Filtering

Per-resource, but spelled consistently:

| Shape | Meaning |
|---|---|
| `?category=Dining` | Equality |
| `?date_from=`, `?date_to=` | Inclusive range, `YYYY-MM-DD` |
| `?q=…` | Free text |

Only ISO dates are accepted. Taking `MM/DD/YYYY` as well would mean the API
silently disagrees with itself about `03/04/2026` depending on which client
sent it — a defect that surfaces as a spending report being wrong by a month.

**The API holds no filter state.** The web transactions page has sticky filters
that survive navigation; that is a browser affordance. Sending no `category`
means *all categories*, always.

---

## Errors

`error.code` is a closed vocabulary and is what a client should switch on.
`error.message` is written for a person and may be reworded at any time —
**never match on it**.

| Code | Status | Meaning |
|---|---|---|
| `bad_request` | 400 | Could not be parsed at all |
| `validation_error` | 422 | Parsed, and the fields are wrong. Carries `details` |
| `unauthenticated` | 401 | No credential, or an unusable one |
| `forbidden` | 403 | Your role does not permit this |
| `insufficient_scope` | 403 | Your *token* does not permit this. Reissue it |
| `csrf_failed` | 403 | A session request without `X-CSRF-Token` |
| `not_found` | 404 | No such resource — or not yours |
| `conflict` | 409 | Well-formed; the resource's state does not permit it |
| `rate_limited` | 429 | Too many attempts |
| `service_unavailable` | 503 | A dependency is not configured or not answering |
| `internal_error` | 500 | Unhandled. Quote `meta.request_id` |
| `method_not_allowed` | 405 | Wrong method for this path |
| `payload_too_large` | 413 | Body exceeded the limit |

Three things worth stating explicitly, because each is a decision rather than an
accident:

**400 and 422 are different.** 400 means the request was malformed; 422 means it
was well-formed and wrong. A 422 is retryable after fixing a field; a 400
usually means the client's request builder is broken. Collapsing them loses the
only signal that distinguishes a user error from a client bug.

**Three codes answer 403,** because the client's response to each differs:
reload and resubmit, reissue the token with more scope, or tell the user to ask
an owner. One status cannot say that.

**404 means "no such resource, or not yours".** Deliberately indistinguishable.
Telling a caller that a row exists but belongs to someone else turns a
sequential id into an oracle for how many transactions another household has.
The same reasoning applies to `401` on a revoked token, which is answered
identically to an unknown one.

---

## Streaming

`POST /chat/conversations/{id}/messages`, `POST /copilot/ask` and
`POST /copilot/investments/ask` answer `text/event-stream`.

```
data: {"delta": "You spent "}
data: {"delta": "$412 on dining"}
data: [DONE]
```

```
data: {"delta": "Looking at "}
data: {"error": "The reply could not be completed."}
```

Contract:

- Zero or more `delta` events, in order.
- At most one `error` event, which is terminal.
- `[DONE]` **only** on successful completion. Its absence after an `error` is
  how a client distinguishes a finished answer from an abandoned one.

Everything decidable *before* the stream opens — a missing message, an unknown
conversation, no model configured — is an ordinary envelope error with an
ordinary status. So a client's normal error path covers every case except a
mid-stream failure, which is the only one that has to be handled specially.

This is the single exception to the envelope, and it is a deliberate trade: a
full answer takes several seconds, and a client showing nothing until it lands
feels broken in a way no amount of consistency compensates for.

### Briefings degrade instead of failing

`GET /copilot/brief` and `/copilot/investments/brief` answer **200** with
`{"available": false}` when no model is configured. A briefing is optional
furniture on a dashboard: a client should render the rest of the page and omit
the card. A 503 would make a healthy page look degraded, and every client would
have to special-case that status to tell "no API key" from "the server is
broken".

The `ask` endpoints do answer 503, because somebody typed a question and pressed
send.

---

## Versioning

`/api/v1` is stable. Within it:

**May happen without notice** — new endpoints, new optional request fields, new
fields in a response object, new enum members in a field documented as open.

**Will not happen** — removing or renaming a field, changing a field's type,
changing what a status code means for an existing endpoint, adding a required
request field, narrowing an accepted value.

Anything in the second list means `/api/v2`, served alongside v1.
`/api/v1/settings` reports `api.supported_versions`, so a client can check that
the version it speaks is still served *before* it is withdrawn — which is what
makes a deprecation announceable in band rather than discovered as a wall of
404s.

Clients should ignore unknown fields rather than rejecting them.

---

## The endpoints that predate v1

The unversioned endpoints — `/api/holdings`, `/api/log/*`, `/api/chat_stream`,
`/api/copilot/*`, `/api/accounts`, `/api/net-worth`, `/api/sync/*` — still
exist, are unchanged, and still serve the web UI.

They are **not deprecated and not removed**. Breaking them to tidy up would be
this phase causing exactly the disruption it exists to prevent. What they are is
*superseded*: they answer in whatever shape suited the page that called them,
and a new client should use v1 instead.

| Unversioned | v1 equivalent |
|---|---|
| `POST /api/holdings`, `PUT|DELETE /api/holdings/{id}` | `/api/v1/investments/holdings…` |
| `GET /api/log/balances`, `PUT /api/log/balances/{type}` | `/api/v1/accounts/balances…` |
| `GET /api/accounts`, `GET /api/net-worth` | `/api/v1/accounts`, `/api/v1/accounts/net-worth` |
| `GET|POST /api/conversations`, `PATCH|DELETE /api/conversations/{id}` | `/api/v1/chat/conversations…` |
| `GET /api/chat_history`, `POST /api/chat_truncate`, `POST /api/chat_clear` | `/api/v1/chat/conversations/{id}/messages` |
| `POST /api/chat_stream` | `POST /api/v1/chat/conversations/{id}/messages` |
| `GET /api/copilot/brief`, `POST /api/copilot/ask` | `/api/v1/copilot/brief`, `/api/v1/copilot/ask` |
| `GET /api/investments/brief`, `POST /api/investments/ask` | `/api/v1/copilot/investments/…` |
| `PUT|DELETE /transactions/{id}`, `POST /transactions/bulk_delete`, `POST /update_categories_bulk` | `/api/v1/transactions…` |

`/api/log/entries*` (the manual check register) and `/api/sync/*`,
`/api/connections*`, `/api/plaid/*` and `/api/institutions` have **no** v1
equivalent yet. The sync endpoints wrap an OAuth flow with a browser redirect in
the middle, which a JSON API cannot usefully offer; `/api/v1/accounts/connections`
reports connection state read-only and a client should send the user to the web
connections page to change it.

---

## Where the code lives

```
dough/api/
  envelope.py    the response shape
  errors.py      the error vocabulary and its handlers
  pagination.py  page/sort parsing, and the stable-ordering rule
  validation.py  reading a JSON body without trusting it
  guard.py       bearer authentication, and the hooks that defer to it
  v1/            one module per resource
```

**No business logic lives in `dough/api/v1/`.** A resource module reads the
request, calls something in `dough/services/`, and shapes a response — the same
services `dough/blueprints/` calls. That is what makes "the API and the page
agree" a structural property rather than a thing that has to be maintained.

`tests/test_services.py::test_api_resource_holds_no_business_logic` enforces it
by rejecting any database write issued from a resource module.

Related reading:

- `docs/adr/0012-versioned-api-contract.md` — why versioned, why opaque tokens,
  what was rejected.
- `docs/security.md` — the findings log, including the API token's threat model.
- `dough/services/README.md` — the dependency rules every service follows.
