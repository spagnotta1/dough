# ADR-0001: Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-07-26

## Context

Dough is moving from a single-owner personal tool to something with an identity
layer, multi-tenancy, a public marketing surface, and an eventual path to
PostgreSQL, OAuth providers, and mobile clients. That transition involves a
number of decisions that are cheap to make now and expensive to revisit later —
household-scoped tenancy, server-side sessions, hand-rolled CSRF and rate
limiting instead of the obvious libraries.

The reasoning behind those choices currently lives in module docstrings and
inline comments. Those are unusually thorough in this codebase, but they are not
discoverable: you find them only if you are already reading the file that
contains them, which means you find them *after* you have formed an opinion, not
before. The specific failure this causes is a future maintainer (or a future
version of the current one) reverting a deliberate choice because it looks like
an oversight — "why isn't this using flask-login?" is a question the code cannot
answer.

## Decision

Significant architectural decisions are recorded as Architecture Decision
Records in `docs/adr/`, numbered sequentially, in the format popularised by
Michael Nygard.

A decision qualifies when it is **structural** (it constrains how other code must
be written), **hard to reverse** (a schema migration, a data backfill, a change
across many call sites), or **surprising** (it deviates from the obvious default,
so the absence of an explanation reads as a mistake).

Routine choices do not get an ADR. Naming, formatting, and anything a reader
would arrive at independently belong in the code.

Each record states the context, the decision, and the consequences — including
the bad ones. An ADR that lists only benefits is marketing, and gets treated as
such by whoever reads it next.

ADRs are immutable once accepted. A decision that no longer holds is superseded
by a new record that links back to it, rather than edited in place; the value is
in the trail, and rewriting history removes exactly the information a reader
needs to understand why the code looks the way it does.

## Consequences

**Good.** Deviations from convention become defensible rather than mysterious.
Onboarding shortens: the "why is it like this" questions have written answers.
Decisions get better, because articulating a trade-off in prose exposes the ones
that were never actually reasoned through.

**Bad.** It is process, and process decays. ADRs written after the fact to
satisfy a checklist are worse than none, because they carry the authority of a
record without the thinking. The mitigation is timing, not discipline: ADRs
0002–0007 are written *during* the phases that make those decisions, while the
alternatives are still live, not batched into a documentation phase at the end.

**Accepted risk.** Some records will turn out to be wrong. That is the intended
behaviour of the format — a superseding ADR that says "we tried this and here is
what it cost" is the most useful document in the directory.

## Index

| ADR | Decision | Phase |
|---|---|---|
| 0001 | Record architecture decisions | 0 |
| 0002 | Household-scoped multi-tenancy | 5 |
| 0003 | ORM event tenant filter as defense-in-depth, fail-closed | 5 |
| 0004 | Server-side sessions over signed cookies | 6 |
| 0005 | argon2id with transparent werkzeug fallback | 6 |
| 0006 | Double-submit CSRF over flask-wtf | 6 |
| 0007 | Alembic as the sole schema authority | 2 |
| 0008 | Fixed marketing palette vs. the app theme system | 8 |
| 0009 | No flask-login, no flask-limiter | 6 |
| 0010 | LLM provider adapter and model catalog | 4 |
| 0011 | Blueprint extraction order | 9 |
