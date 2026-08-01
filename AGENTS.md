# Dough Financial — AI Coding Agent Instructions

This repository is the official codebase for Dough Financial.

Read this before changing code. Where it disagrees with a test, the test wins —
tell the human rather than editing the test to agree with you.

---

## What this is, technically

A **Flask** application. Server-rendered **Jinja** templates, **SQLite** via
SQLAlchemy, **Alembic** for schema, **Tailwind** for styling, **Alpine.js** for
interactivity, **Chart.js** for charts. Python 3.10/3.11.

There is **no React, no build step, and no npm**. If an instruction anywhere
tells you to reach for Framer Motion, GSAP, a component library, or to worry
about re-renders, it is describing a different application. Animation here is
CSS, and behaviour is vanilla JS or a small Alpine component.

```
AGENTS.md               this file — read before changing anything
app.py                  the factory: config, db, request hooks, error handlers
dough/blueprints/       every HTML route, grouped by responsibility
dough/api/              the versioned JSON API — envelope, errors, auth, v1/
dough/services/         the queries and rules, callable without a request
dough/ai/               the model provider, prompts, caching, output formatting
dough/auth.py           authentication, authorization, CSRF
dough/tenancy.py        household scoping and the ORM backstop
models.py               the schema
finance_sync/           the institution adapters and the sync pipeline
static/js/dough.js      the mascot artwork
static/css/dough.css    the mascot's theme tokens, components and animations
```

### Before you report a change as done

```bash
python -m pytest -q                     # the suite; must be green
python -m pytest tests/browser -q       # if you touched templates, CSS or JS
python tools/build_dough_assets.py      # if the mascot artwork changed…
python tools/build_icons.py             # …then always this too
```

The browser suite needs `requirements-browser.txt` and a Chromium download; if
Playwright is absent it skips wholesale rather than failing. Say so explicitly
if you could not run it.

---

## Priorities

In this order, and the first is never traded for the others:

1. Don't leak one household's data into another's.
2. Preserve Dough's brand identity.
3. Don't break existing functionality.
4. Improve the user experience.
5. Keep the interface simple.
6. Keep the codebase clean and maintainable.
7. Improve performance where it is measurably worth it.

---

## Dough the mascot (CRITICAL)

**The ONLY approved mascot artwork is `brand/dough-master.jpg`.**

It is a finished brand asset and the single source of truth. Everything the app
renders is a **crop or a scale of that file** — never a drawing of it.

The master is **archival**: it lives outside `static/`, is never served, and is
read only by the build scripts. What the application renders is the generated,
background-removed PNGs, which are committed. Background removal is not part of
normal development — you run it when the artwork changes, and never otherwise.

| File | What it is |
| --- | --- |
| `brand/dough-master.jpg` | The artwork. Archival source of truth. Not served. |
| `static/img/dough.png`, `dough@2x.png` | Full seated puppy, background removed. Heroes, empty states, 404, onboarding. |
| `static/img/dough-head.png`, `dough-head@2x.png` | Square crop of head + ears, for the `.dough-avatar` discs. |
| `static/img/dough-mark.png` | **The frozen mark** — eyes and nose. Tab-size icons only. |
| `tools/build_dough_assets.py` | Produces all of the above from the master. The only thing allowed to write them. |
| `tools/build_icons.py` | Composites them onto tiles to make every favicon and app icon. |

`static/js/dough.js` **places** the artwork — which crop, what size, what
label — and holds no coordinate of Dough.

### The mark

A mascot illustration does not survive 16×16: the full dog, and even the head
crop, collapse into a brown smudge. `dough-mark.png` is Dough's simplified
mark — his eyes and nose — and it is what the 16/32/48px favicons and the
`.ico` carry.

It is a **crop of the approved artwork**, not a second drawing, and it is
**frozen**. Do not re-derive it on taste. If a genuinely new mark is ever
wanted — a monogram, a paw print, a nose silhouette — that is new artwork and
needs a human designer; it is not something to generate.

### Never

- Redraw, re-trace, or "improve" Dough. **A trace is a redraw**, however
  carefully it is measured. This already happened once: ~13 hand-authored SVG
  paths traced from the reference shipped a visibly different dog — narrow
  strap ears instead of plush flared ones, a smooth dome where the reference
  has fur scalloping on the cheeks, a small nose and tongue against the
  reference's wide open smile. Nothing failed; it just was not the brand asset.
- Generate a new mascot, a substitute, an approximation, or something "similar".
- Generate SVG artwork, CSS artwork, or Canvas artwork of Dough.
- Replace him with clip art, an emoji, an AI-generated image, or a stock icon.
- Re-tint, recolour, or filter the artwork. He is the same golden puppy on all
  16 themes; the theme shows up in the disc, the bubble and the panel behind
  him — **the chrome around him, never him**.
- Hand-edit the derived PNGs. Change the JPG and re-run the two build scripts.
- Choose a crop by eye. Two hand-picked crops have already shaved his ear tips
  into a flat vertical edge. `head_box()` in `tools/build_dough_assets.py`
  measures it from the silhouette's own width profile, and
  `tests/test_dough_mascot.py` enforces the result.
- Hardcode `width`/`height` on a `.dough-avatar`. Sizes come from
  `--dough-size`, which is how they stopped drifting apart.

Treat the artwork the way you would treat the Nike swoosh.

The tests enforce this, not just this document:
`test_the_mascot_is_not_drawn_in_javascript` rejects any `<path>`/`<svg>` in
`dough.js`, and `test_no_drawn_mascot_survives_in_static` fails if a vector
`app-icon.svg` reappears.

### Allowed

Placement, spacing, responsiveness, accessibility, loading and lazy-loading,
shadows, glow, hover, page transitions, entrance and idle animation — floating,
breathing, a slight bounce, soft hover scaling, opacity, an animated ground
shadow.

None of these may modify the artwork. The drawing must stay visually identical.
Motion moves the image; it never modifies it. Anything that pushes an ear past
the avatar disc is clipping, not animation, and anything drawn *on top of* him
(a sparkle overlay, a prop, a badge) is new mascot artwork by another name.

### Placing him

**Use the macro. Do not hand-write a `<span data-dough>`.**

```jinja
{% from '_dough.html' import dough, dough_avatar, dough_working %}

{{ dough('celebrate', size=240) }}
{{ dough_avatar('idle', 'sm') }}
{{ dough_working('Reading your transactions') }}
```

`templates/_dough.html` is the one place that knows how Dough is sized,
labelled, discs, and animates. That is what keeps those single decisions
instead of twenty copies of a convention — the avatar sizes drifted apart
(22/23/24/30/56px) once already.

### States, not poses

A state is semantic: it says what the *product* is doing. It selects a motion
and sometimes a piece of UI beside him. It never selects a different Dough,
because there is one pose and there will be one until somebody commissions
layered artwork.

| State | Motion | Extra |
| --- | --- | --- |
| `idle` | float | — |
| `loading` / `thinking` | head tilt | thinking dots |
| `searching` | side-to-side | thinking dots |
| `celebrate` | bounce | page confetti |
| `success` / `wave` | bounce | — |
| `sleep` | slow breathe | — |

The older names (`happy`, `greeting`, `curious`, `concerned`, `celebrating`, …)
are aliases onto these, so existing pages did not need rewriting.

Adding a state is one row in `STATES`. Adding a pose is not possible, and is
not supposed to be.

The dots and confetti are **UI, not artwork** — siblings of the `<img>`, never
overlaid on it. Anything drawn on top of Dough is new mascot artwork by another
name, and `test_the_state_effects_are_ui_not_artwork` rejects it.

There is **no tail wag**. It rotated the tail path, and a raster has no parts —
a fake wag reads worse than none. If layered artwork (tail / body / ears / eyes
/ mouth) is ever commissioned, that is the first thing to animate.

### Tone of motion

Dough should feel alive and calm. Never hyperactive, distracting, continuously
spinning, vibrating, distorted, stretched, squashed, or heavily rotated. All
motion is switched off under `prefers-reduced-motion` — see the bottom of
`dough.css`, and keep new animation inside that block's reach.

### Theming

Dough does not theme. He is a photograph of a fixed brand asset, and re-tinting
a raster means altering the artwork.

This inverts a constraint rather than removing it: a themed mascot could always
be pushed away from a panel he clashed with, and a fixed one cannot. So if a
palette's `--panel` drifts toward his fur he turns into a smudge, and the fix is
to **change that theme's panel, never the artwork**.
`tests/browser/test_dough_theming.py` samples his actual rendered pixels in a
real engine and fails any theme where he stops separating from the surface.

### Where he appears

Landing, login, loading states, chat (`Ask Dough`), dashboard (`Dough's
Insight`), investments (`Dough's Portfolio Review`), the Budget Coach line,
empty states, success messages, onboarding, 404s, and the waiting state of every
model call.

He is **never the only signal** — every mascot state has text beside it, because
an expression alone carries nothing to a screen reader. And don't overuse him:
his presence should read as intentional.

The only attribute `dough.js` hydrates is `data-dough="<mood>"`. An unknown mood
falls back to `happy` silently, and `data-dough-expression` renders nothing at
all — a real bug that shipped on the error page once.

---

## Architecture

Business logic lives in `dough/services/`. Routes read the request, call a
service, and shape a response. Four rules, each with a test behind it:

- **A blueprint may not import `app`.** What a route needs from the application
  it gets from `current_app`; what it needs from the domain it gets from a
  service. `app` imports the blueprints, so the reverse is a cycle.
- **A service may not import `app`, `anthropic`, or Flask's response helpers.**
  A service returns data. A service that renders a template cannot be called by
  the scheduler.
- **Endpoint names are `blueprint.view`** — `url_for('transactions.index')`.
  URLs are frozen by `tests/test_url_map_snapshot.py`; a new page needs a line
  in its `EXPECTED_RULES`.
- **No business logic in `dough/api/v1/`.** A resource module calls the *same*
  service the HTML blueprint calls, which is what makes "the API and the page
  agree" structural.
  `tests/test_services.py::test_api_resource_holds_no_business_logic` rejects
  any database write issued from a resource module.

Adding a page: a view in the right blueprint, its query in a service, a line in
`EXPECTED_RULES`. Adding an API endpoint: the same, plus an entry in
`docs/api/openapi.yaml` **in the same commit** — `tests/test_openapi.py` fails a
route that is served but undocumented, and a documented one that is not served.

If a change needs `app.py`, it is probably wiring. If it isn't wiring, it
probably belongs somewhere else.

Prefer readable code, small functions, descriptive names, and reuse over new
abstraction. If similar code exists, call it.

---

## Tenancy — the rule that matters most

A **household** is the unit of isolation. No query may return another
household's rows. `dough/tenancy.py` holds the scoping and an ORM backstop, and
`tests/test_tenancy_boundary.py` polices it.

`audit_events` is the one table whose `household_id` is nullable (a failed login
belongs to no household), which puts it *outside* the backstop —
`dough.services.audit.recent()` **is** its isolation. Read it through that
function and nothing else. See ADR-0011.

The audit trail is append-only, enforced by a SQLAlchemy hook. Do not add a
code path that rewrites it.

---

## Schema changes

Alembic is the sole schema authority (ADR-0007). Never hand-edit the database
or add columns in application code.

A migration that **rebuilds** a table can lose rows while leaving a perfectly
valid schema behind, so it is verified against a copy before it touches
anything real. The full ceremony — backup, baseline row counts, migrate a copy,
`tools/verify_tenancy.py`, only then the real database — is in the README under
"Applying the multi-tenancy migration". Follow it; don't improvise a shorter
version.

---

## Security

- CSRF is always on and has no environment switch. Every unsafe request carries
  the session token, including sign-in, registration and password reset.
- Tokens, invitation links and reset links are shown **once** and stored only as
  hashes. Don't add a "resend the same link" path — there is nothing to resend.
- **Never logged:** passwords, API keys, tokens, account numbers, query strings,
  AI prompts and completions, and tracebacks (type and message only). Redaction
  runs at the formatter so it covers lines nobody thought about. Don't route
  around it.
- Responses that would reveal whether an address has an account must stay
  identical in words, page and timing. `/forgot-password` is built this way
  deliberately.
- Never commit a secret. `tests/test_secret_hygiene.py` checks.
- Health: `/health/live` for restart policies, `/health/ready` for load
  balancers. Never point a restart policy at `/health/ready`.

---

## Deployment

**Exactly one application worker.** The background sync scheduler starts lazily
inside the serving process, so two workers means two schedulers hitting the same
provider rate limits and interleaving writes into one `sync_history`. Nothing
reports this; the first environment where it exists is production. See `OPS-0012`
in `docs/security.md`.

---

## UI and UX

The app should feel closer to ChatGPT than to traditional banking software:
spacious, clean, modern, calm, conversational. Whitespace is a feature. Every
screen answers "what is the user trying to accomplish?" — if an element doesn't
earn its place, remove it.

Colour: warm neutrals, clean whites, subtle gradients, modern shadows, soft
rounded corners. Not: excessive colour, harsh borders, visual noise, anything
that flashes.

Every feature works on desktop, tablet and phone. Never build a desktop-only
interface. `tests/browser/` catches horizontal overflow, dialogs that don't
open, focus that lands nowhere, and JavaScript that throws.

**Accessibility is not optional.** Alt text on every image, accessible labels on
every control, keyboard navigation that works, contrast that passes, and motion
that respects `prefers-reduced-motion`. `dough/contrast.py` and
`tests/test_contrast.py` exist because contrast regressions are invisible until
someone can't read the page.

---

## Errors and the AI voice

Never fail silently. Show the user a friendly message, log something a developer
can act on, and expose no internals. Every response carries `X-Request-ID` and
error pages show the same trace id, so one string finds everything.

Dough's voice is knowledgeable, helpful, calm, professional, efficient. He
encourages and never shames — going over budget is a fact to work with, not a
failing. No fear, no dog puns, no emoji, no filler openers, no repetition. Every
generated word in the app opens with the shared persona in `dough/ai/persona.py`
so chat, the dashboard insight, the portfolio review and the briefing read as one
companion rather than four assistants.

---

## Before you finish

1. Is every pixel of Dough still coming from `dough_V2.jpg`?
2. Could this query cross a household boundary?
3. Does the API still match `openapi.yaml`?
4. Does it work on a phone, with a keyboard, and under reduced motion?
5. Did I add duplication, a hardcoded value that should be configuration, or an
   abstraction with one caller?
6. Is `python -m pytest -q` green?

If an implementation requires modifying or recreating Dough's artwork, the
implementation is wrong. Choose another solution.
