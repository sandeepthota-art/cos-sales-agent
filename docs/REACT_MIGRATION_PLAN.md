# React + FastAPI Migration Plan — CoS Staff EA Agent

**Status: DESIGN ONLY. No implementation code in this document has been written
to the codebase. No database migration, no `--confirm`, no CTO database
access, no change to the frozen Emails/Threads/People schema occurred in
producing this plan.**

This plan is the required written design for migrating the user-facing
dashboard from Streamlit to React + TypeScript, backed by a new FastAPI REST
layer, per the explicit investigation-first requirement. It must be reviewed
and approved before any implementation begins.

---

## 1. Current Architecture

```
Streamlit dashboard (app/ui/dashboard.py)
    ↓ calls directly, in-process
app/ui/data.py (list_* functions, some with UI-layer joins/enrichment)
    ↓
app/database/repositories.py (_BaseRepository subclasses, one per collection)
    ↓
MongoDB (via app.database.mongodb.get_client)

MCP server (app/mcp/server.py, 25 tools)
    ↓
app/mcp/tools.py (tool implementations)
    ↓
app/database/repositories.py (same repositories, same MongoDB)

app/query/ (service.py, retrieval.py, evidence.py, synthesis.py,
            meetings.py, commitments.py, entity_resolution.py, intent.py)
    -- an ALREADY-EXISTING, already-decoupled business-logic layer,
       used today only by the `ask_question` MCP tool, but written with
       zero Streamlit/MCP-specific coupling.
```

**Two deployed Render services today, same Docker image, different start commands:**
1. MCP server — `python -m app.mcp.server` (stdio or `streamable-http` depending on `$PORT`/`MCP_TRANSPORT`)
2. Dashboard — `streamlit run app/ui/dashboard.py --server.port $PORT --server.address 0.0.0.0 --server.headless true`

**No REST API, no React, no Node.js, no `package.json` exist anywhere in this repo today** (confirmed by direct filesystem search).

### Layer separation (A-E, as requested)

| Layer | Files | Notes |
|---|---|---|
| **A. Presentation** | `app/ui/dashboard.py` (`_render_*_tab` functions), `app/ui/column_descriptions.py` | Streamlit-specific. Replaced by React. |
| **B. API/data-access** | `app/ui/data.py` (`list_*` functions + 2 UI-layer joins: `list_follow_ups`'s `what`/`person_name`/`org_name`, `list_meetings`'s `title`) | **Already framework-agnostic** — no Streamlit import, pure functions taking `db`. Directly importable by FastAPI. |
| **C. Business logic** | `app/query/*`, `app/replies/approval.py`, `app/calendar/actions.py`, `app/entities/resolution.py`, `app/pipeline.py` | Already decoupled from both Streamlit and MCP. The strongest reuse candidate for the new API. |
| **D. Repository/DB** | `app/database/repositories.py`, `app/database/mongodb.py`, `app/database/indexes.py` | Untouched by this migration. |
| **E. MCP-specific** | `app/mcp/server.py`, `app/mcp/tools.py` | Untouched by this migration — continues using the same repositories/services independently. |

**Conclusion: nothing in Layer B or C needs to be rewritten or duplicated.** The new FastAPI layer is almost entirely thin route handlers calling into B/C directly — the same pattern MCP tools already use.

---

## 2. Target Architecture

```
React + TypeScript (Vite)
    ↓ HTTPS, same-origin (see §12)
FastAPI (new: app/api/)
    ↓ imports directly, no duplication
app/ui/data.py  +  app/query/*  +  app/replies/approval.py  +  app/calendar/actions.py
    ↓
app/database/repositories.py
    ↓
MongoDB

MCP server: UNCHANGED, independent process, same repositories/services.
```

FastAPI chosen over Flask: native Pydantic request/response models (the
codebase already has ~15 Pydantic schema files — `app/query/schemas.py` maps
almost directly onto FastAPI response models), native async support, automatic
OpenAPI docs (useful for the React client's typed API layer), and it is the
natural Python-ecosystem peer to the MCP SDK already in use (both are
ASGI-based, both already depend on `starlette`/`uvicorn` — **zero new core
dependencies**, confirmed via `requirements.txt`: `starlette`, `uvicorn` are
already pinned for the MCP server's `streamable-http` transport).

**No Node.js backend of any kind.** React only ever talks to this one FastAPI
service.

---

## 3. API Endpoint Inventory

All endpoints prefixed `/api/v1`. "Service used" names the existing Python
function the route calls — **no endpoint re-implements a MongoDB query that a
service/repository already does.**

### Dashboard Overview
| Method | Route | Purpose | Params | Response | Service used | Auth | Paginated? | Filter/sort? |
|---|---|---|---|---|---|---|---|---|
| GET | `/overview` | Metrics + "what's on my table" | — | counts + pending items | `app.ui.data.dashboard_metrics` + `app.mcp.tools.whats_on_my_table` | required | no | no |

### Emails (FROZEN schema — read-only endpoints only)
| Method | Route | Purpose | Params | Response | Service used | Auth | Paginated? | Filter/sort? |
|---|---|---|---|---|---|---|---|---|
| GET | `/emails` | List emails, newest-first | `limit`, `offset`, `search` | `Email[]` (frozen column set) | `app.ui.data.list_emails` (add offset/limit slicing) | required | **yes (new)** | search by subject/sender (new, server-side) |
| GET | `/emails/{message_id}` | One email's detail | — | `Email` | `EmailRepository.find_one` | required | no | no |
| GET | `/threads/{thread_id}` | Full thread (all messages + latest context) | — | thread + messages + context | `app.mcp.tools.get_thread` | required | no | no |

### People
| Method | Route | Purpose | Params | Response | Service used | Auth | Paginated? | Filter/sort? |
|---|---|---|---|---|---|---|---|---|
| GET | `/people` | List, approved column order | `limit`, `offset`, `org`, `email` | `Person[]` | `app.ui.data.list_people` (add filter params, mirrors `app.mcp.tools.list_people`'s existing filter logic) | required | yes (new) | org/email filter (existing in MCP tool, just not yet in data.py) |
| GET | `/people/{id}` | Person detail/360 view | — | person + context + relationships | `app.entities.context.get_person_context` (already exists, used by `retrieve_person_context`) | required | no | no |

### Organizations
| Method | Route | Purpose | Params | Response | Service used | Auth | Paginated? | Filter/sort? |
|---|---|---|---|---|---|---|---|---|
| GET | `/organizations` | List | `limit`, `offset` | `Organization[]` | `app.ui.data.list_organizations` | required | yes (new) | no |
| GET | `/organizations/{id}` | Org 360 view | — | org + people + projects + opportunities | `app.mcp.tools.get_company_summary` | required | no | no |

### Projects
| Method | Route | Purpose | Params | Response | Service used | Auth | Paginated? | Filter/sort? |
|---|---|---|---|---|---|---|---|---|
| GET | `/projects` | List | `limit`, `offset`, `entity`, `goal_pillar` | `Project[]` | `app.ui.data.list_projects` (add filters, mirrors `tools.list_projects`) | required | yes (new) | entity/goal_pillar (existing in MCP tool) |
| GET | `/projects/{id}` | Project detail | — | project + summary | `app.mcp.tools.get_project_summary` | required | no | no |
| **PATCH** | `/projects/{id}` | **Update status/owner/health/next_milestone/due** | body: any subset of those 5 fields | updated `Project` | `app.mcp.tools.update_project_fields` | required | no | no |

### Opportunities
| Method | Route | Purpose | Params | Response | Service used | Auth | Paginated? | Filter/sort? |
|---|---|---|---|---|---|---|---|---|
| GET | `/opportunities` | List | `limit`, `offset`, `org_id`, `status`, `project_id` | `Opportunity[]` | `app.ui.data.list_opportunities` (add filters, mirrors `tools.list_opportunities`) | required | yes (new) | org_id/status/project_id (existing) |
| GET | `/opportunities/{id}` | Detail | — | `Opportunity` | `OpportunityRepository.find_one` | required | no | no |
| **PATCH** | `/opportunities/{id}` | **Update stage/owner/value/currency/expected_close_date/next_action** | body: any subset | updated `Opportunity` | `app.mcp.tools.update_opportunity_fields` | required | no | no |

### Commitments
| Method | Route | Purpose | Params | Response | Service used | Auth | Paginated? | Filter/sort? |
|---|---|---|---|---|---|---|---|---|
| GET | `/commitments` | List | `limit`, `offset`, `thread_id`, `class_`, `project_id`, `person_id`, `org_id` | `Commitment[]` | `app.ui.data.list_commitments` + `app.query.commitments.*` (person/org/open/overdue variants already exist) | required | yes (new) | multiple (existing in query layer) |

### Follow-ups
| Method | Route | Purpose | Params | Response | Service used | Auth | Paginated? | Filter/sort? |
|---|---|---|---|---|---|---|---|---|
| GET | `/follow-ups` | List, enriched (`what`/`person_name`/`org_name`) | `limit`, `offset`, `status`, `escalation_level`, `audience` | `FollowUp[]` (enriched) | `app.ui.data.list_follow_ups` directly (already does the join) | required | yes (new) | `app.query.commitments.get_followups_by_escalation_level`/`get_followups_by_audience` already exist for server-side filtering |

### Meetings
| Method | Route | Purpose | Params | Response | Service used | Auth | Paginated? | Filter/sort? |
|---|---|---|---|---|---|---|---|---|
| GET | `/meetings` | List, enriched (`title`) | `limit`, `offset`, `category`, `date_from`, `date_to` | `Meeting[]` (enriched) | `app.ui.data.list_meetings` directly; `app.mcp.tools.list_meetings`'s existing category/date filters need porting into this endpoint or `data.py` | required | yes (new) | category/date (existing in MCP tool) |
| GET | `/meetings/{id}/brief` | Meeting brief (prep view) | — | `MeetingBrief` | `app.query.meetings.get_meeting_brief` | required | no | no |

### Personal Items
| Method | Route | Purpose | Params | Response | Service used | Auth | Paginated? | Filter/sort? |
|---|---|---|---|---|---|---|---|---|
| GET | `/personal-items` | List | `limit`, `offset` | `PersonalItem[]` | `app.ui.data.list_personal_items` | required | yes (new) | no |

### Thread Explorer
| Method | Route | Purpose | Params | Response | Service used | Auth | Paginated? | Filter/sort? |
|---|---|---|---|---|---|---|---|---|
| GET | `/threads` | List (for the thread picker) | `limit`, `offset` | `Thread[]` | `app.ui.data.list_threads` | required | yes (new) | no |
| GET | `/threads/{thread_id}/context-versions` | All context snapshot versions | — | `ContextSnapshot[]` | `app.ui.data.thread_context_versions` | required | no | no |

### Context Evolution
(Same two endpoints above, re-used — this is a presentation choice in React, not a new backend surface.)

### Knowledge
| Method | Route | Purpose | Params | Response | Service used | Auth | Paginated? | Filter/sort? |
|---|---|---|---|---|---|---|---|---|
| GET | `/knowledge` | List facts | `limit`, `offset`, `thread_id` | `KnowledgeItem[]` | `app.ui.data.list_knowledge` | required | yes (new) | thread_id (existing) |

### Reply Approval
| Method | Route | Purpose | Params | Response | Service used | Auth | Paginated? | Filter/sort? |
|---|---|---|---|---|---|---|---|---|
| GET | `/reply-drafts` | List pending (+ resolved person/org/thread context) | `status` (default `awaiting_approval`) | `ReplyDraft[]` enriched | `app.ui.data.list_reply_drafts` + the `_person_label`/`_org_label`/source-email-fallback logic currently inline in `dashboard.py` (promote to `app/ui/data.py` or a new `app/api/services/reply_drafts.py`) | required | no (small set by design) | status |
| **POST** | `/reply-drafts/{reply_id}/approve` | Approve + simulate-send | — | updated `ReplyDraft` | `app.replies.approval.approve` + `simulate_send`, then `ReplyDraftRepository.upsert_by_key` (exact orchestration currently in `dashboard.py`) | required, **and must respect `dashboard_read_only`** | no | no |
| **POST** | `/reply-drafts/{reply_id}/reject` | Reject | — | updated `ReplyDraft` | `app.replies.approval.reject` + upsert | required + read-only check | no | no |
| **POST** | `/reply-drafts/{reply_id}/edit` | Edit body/subject | body: `{subject, body}` | updated `ReplyDraft` | `app.replies.approval.edit` + upsert | required + read-only check | no | no |

### Calendar Approval
| Method | Route | Purpose | Params | Response | Service used | Auth | Paginated? | Filter/sort? |
|---|---|---|---|---|---|---|---|---|
| GET | `/calendar-actions` | List pending + needs-clarification (+ resolved person/org context) | `status` | `CalendarAction[]` enriched | `app.ui.data.list_calendar_actions` + the `_person_label`/`_org_label` logic (same promotion as above) | required | no | status |
| **POST** | `/calendar-actions/{thread_id}/{meeting_fingerprint}/approve` | Create on calendar | — | updated `CalendarAction` | `app.calendar.actions.approve_calendar_action` (needs `calendar_provider` injected via FastAPI dependency, not constructed inline) + upsert | required + read-only check | no | no |
| **POST** | `/calendar-actions/{thread_id}/{meeting_fingerprint}/ignore` | Reject | — | updated `CalendarAction` | `app.calendar.actions.reject_calendar_action` + upsert | required + read-only check | no | no |

**Pagination note:** none of the existing `app/ui/data.py` functions paginate
today (Streamlit renders the full list client-side) — every "yes (new)"
above is a genuinely new addition (offset/limit added to the repository
query), required by this plan's own performance section (§11), not
duplicated logic.

---

## 4. Authentication Design

**Current mechanism (confirmed by direct read of `dashboard.py` +
`settings.py`): trivially insecure for a real API** — a single optional
plaintext `dashboard_password` compared with `==`, unlocked via Streamlit's
own `session_state` (not a cookie/token), no hashing, no rate limiting, no
expiry. Fine for a Streamlit prototype behind an unlisted URL; not acceptable
to carry forward verbatim into a public REST API.

**Recommendation — simplest production-appropriate model, matching this
project's single-operator-tool nature** (not a multi-tenant SaaS, so no need
for a full user-accounts system):

- **FastAPI side:** one shared operator password (upgraded to a hashed
  comparison, e.g. `passlib`/`bcrypt` — new dependency, minimal), exchanged at
  `POST /api/v1/auth/login` for a short-lived, signed, `httpOnly`, `Secure`
  session cookie (JWT or an opaque session id — JWT avoids needing a session
  store, consistent with "simplest"). Every other route depends on a FastAPI
  `Depends(require_auth)` that validates that cookie. A new `api_secret_key`
  and `api_session_ttl_minutes` setting are added to `Settings` (confirmed:
  **no such fields exist today**, this is pure addition, not a rename of
  anything existing).
- **`dashboard_read_only`** carries forward as-is, unchanged in meaning —
  every mutation route checks it server-side (never trust a client-side
  disabled-button alone) exactly like `dashboard.py`'s render functions do
  today.
- **React side:** the cookie is `httpOnly` — React never reads or stores the
  token itself, the browser just sends it automatically on same-origin
  requests. No token ever lives in `localStorage`/JS-accessible storage.
- **CORS:** if React is served as **static files from the same FastAPI
  service** (recommended, §12), CORS is a non-issue (same origin). If served
  separately, a new `cors_allowed_origins` setting restricts
  `Access-Control-Allow-Origin` to exactly that one deployed React origin —
  never `*`.
- **MongoDB credentials and `MCP_AUTH_TOKEN` are never sent to React under
  any circumstance** — they stay server-side-only, exactly as today; React
  only ever holds a session cookie scoped to the new FastAPI auth, which has
  zero relationship to Mongo credentials or the MCP bearer token.
- **MCP authentication is completely untouched** — `_BearerAuthMiddleware`,
  `MCP_AUTH_TOKEN`, `X-Auth-Token` fallback all continue exactly as-is,
  independent of this new API auth system. The MCP server is not modified by
  this plan at all.
- **HTTPS:** enforced at the Render platform layer (Render terminates TLS
  for every service automatically) — no new work needed, same as today's two
  services.

---

## 5. React Architecture

**Stack:** React + TypeScript + Vite (not Next.js — no SSR/file-routing
need for an internal tool behind auth) + React Router + a hand-written typed
API client (not a heavy codegen tool — the API surface above is modest
enough that a thin `fetch` wrapper per resource, typed against
hand-mirrored interfaces from `app/query/schemas.py`/the Pydantic models, is
simpler to maintain than introducing an OpenAPI-codegen build step). No
Redux/MobX — React Query (`@tanstack/react-query`) for server-state
caching/loading/error states (directly serves §11's caching/pagination/
debounce requirements) + plain `useState`/`useContext` for local UI state
(selected tab, drawer open/closed, filters). This is a deliberately small
dependency set — no "unnecessary libraries," per your instruction.

```
src/
  api/              # one file per resource: emails.ts, people.ts, projects.ts, ...
                     # thin fetch wrappers + React Query hooks (useEmails(), useProject(id), ...)
  types/             # TS interfaces mirroring app/query/schemas.py + entity models
  components/        # AppShell, Sidebar, TopBar, PageHeader, DataTable, StatusBadge,
                     # EmptyState, LoadingSkeleton, ErrorState, Modal, Drawer, Search, FilterBar
  features/
    emails/          # EmailList, EmailDetail, ConversationView
    people/          # PersonTable, PersonDetail
    organizations/
    projects/        # ProjectTable, ProjectDetail, ProjectFieldsEditor
    opportunities/
    commitments/
    followups/
    meetings/        # MeetingTable, MeetingBrief
    personal-items/
    threads/         # ThreadExplorer
    context/         # ContextEvolution timeline
    knowledge/        # KnowledgeCardList
    approvals/       # ReplyApprovalCard, CalendarApprovalCard
  pages/             # one per route, composes features/* + layout
  layouts/           # AppShell wrapper used by every page
  hooks/             # useDebouncedValue, useAuth, etc.
  utils/             # formatting (dates, status-badge color mapping), etc.
```

---

## 6. CTO Dashboard UX

Rebrand (**"CoS Staff EA Agent"**, subtitle **"Chief of Staff • Executive
Assistant"**) carries forward unchanged from the Streamlit version — same
copy, now in the React `AppShell`/`Sidebar` header instead of
`st.title`/`st.caption`. Visual language: restrained palette (1-2 accent
colors for status badges, neutral grays/whites otherwise — no "dashboard
template" gradients per your explicit anti-goal list), cards with subtle
borders/shadow rather than heavy color blocks, consistent `StatusBadge`
component reused across every entity (open/won/lost, awaiting_approval/
approved/rejected, active/resolved/dropped, etc. — one component, mapped
per-domain status vocabulary).

---

## 7. Collection/Tab Structure — Preserved

All 14 areas map 1:1 to React pages, same order as the current Streamlit
`st.tabs(...)` list and the previously-approved column orders
(`EMAIL_COLUMN_ORDER`, `PEOPLE_COLUMN_ORDER`, `PROJECTS_COLUMN_ORDER`, etc. in
`app/ui/column_descriptions.py`) — these constants are **reused as the
source of truth for the API response field order / React table column
config**, not redesigned. The underlying MongoDB schema is untouched by this
plan (confirmed: no repository, model, or migration file is listed in §15/16
below).

---

## 8. Email Experience

Inbox-style list (newest-first, reusing `GET /emails`) → click a row → full
thread view (`GET /threads/{thread_id}` → `get_thread`'s existing
`messages[]` + `event_trail` + `latest_context`) → shows related people/org
(resolved client-side from `person_ids`/`org_ids` already present on the
thread document, via the People/Organizations endpoints) → any reply draft
for that thread (`GET /reply-drafts?thread_id=...`, a new filter param
mirroring the existing `thread_id` filter `list_reply_drafts` already
supports as a repository query). **Frozen schema fields only** —
`EMAIL_COLUMN_ORDER` is the field contract this view is built against, not a
redesign of it.

---

## 9. Entity Detail Experience

Detail drawers/pages for Person, Organization, Project, Opportunity, each
backed by an **already-existing** cross-entity function — no new
relationship logic invented:

- Person → `app.entities.context.get_person_context` (org, recent activity,
  open threads, related commitments/meetings — already assembles exactly this)
- Organization → `app.mcp.tools.get_company_summary` (people, projects,
  commitments via project linkage)
- Project → `app.mcp.tools.get_project_summary` (commitments, follow-ups)
- Opportunity → direct fetch + its own `project_ids`/`person_ids`/
  `meeting_ids`/`source_email_ids` arrays, each resolved via the
  corresponding list endpoint (no new cross-collection query needed — the
  Opportunity document already carries every relationship id)

---

## 10. Approval Workflows

Both workflows preserved exactly, calling the new POST endpoints in §3 (never
MongoDB directly from React). `ReplyApprovalCard`/`CalendarApprovalCard`
components reproduce the exact field set already shown in the improved
Streamlit cards (recipient/org/thread/drafted-time for replies;
person/org/thread for calendar) — this was already designed for CTO
readability in the prior Streamlit pass, so React inherits that design
rather than re-deriving it.

---

## 11. Performance

- **Server-side pagination**: every list endpoint takes `limit`/`offset`,
  translated to MongoDB `.skip()/.limit()` in the repository layer (a small,
  additive change to `_BaseRepository.find_many`, not a rewrite).
- **Server-side filtering**: reuses filter parameters the MCP tools already
  implement (`org`, `email`, `entity`, `goal_pillar`, `status`, `thread_id`,
  etc.) — ports them into the corresponding FastAPI route's query params.
- **Debounced search**: client-side debounce (300ms) before calling a new
  `search` param on `/emails` and future entity search endpoints.
- **Caching**: React Query's built-in stale-while-revalidate cache handles
  this without custom backend caching initially; revisit only if profiling
  shows a need.
- **No N+1**: the two existing UI-layer joins (`list_follow_ups`,
  `list_meetings`) already batch their lookups reasonably (one query per
  referenced thread/commitment, not per-row) — this plan preserves that
  pattern rather than introducing new N+1 risk, and the new Person/Org detail
  endpoints each make a bounded, small number of queries (confirmed by
  reading `get_person_context`/`get_company_summary`'s existing
  implementations — already written with this concern in mind).

---

## 12. Deployment Architecture

**Recommended: Option A — single new Render service, FastAPI serving both
the API and the built React static files** (`StaticFiles` mount for
everything under `/`, API routes under `/api/v1/*`). Rationale: same
Docker image/base as today (`python:3.11-slim-bookworm`), no CORS to
configure at all (same origin), one fewer Render service to pay for/manage
than a split deployment, and Render's existing `$PORT` convention carries
over unchanged (`uvicorn app.api.main:app --host 0.0.0.0 --port $PORT`,
mirroring the dashboard's existing `--server.port $PORT` pattern).

- **Services after migration: 3 total** — MCP server (unchanged), this new
  combined API+React service, and the existing Streamlit dashboard kept
  running in parallel until §13's cutover criteria are met.
- **Build step**: React's `npm run build` output (static `dist/`) gets
  copied into the Docker image alongside the Python app (new Dockerfile
  stage or a multi-stage build — minimal addition, same base image).
- **Local dev**: `npm run dev` (Vite dev server, proxying `/api` to a
  locally-running `uvicorn app.api.main:app --reload`) — standard Vite
  pattern, no new infra needed locally.
- **Env vars (new)**: `API_SECRET_KEY`, `API_SESSION_TTL_MINUTES`, and (only
  if the split-service alternative is ever chosen later) `CORS_ALLOWED_ORIGINS`.

---

## 13. Migration Strategy

1. Build FastAPI (§3's read endpoints first) as new code, additive only —
   zero changes to `app/ui/dashboard.py`, zero changes to MCP.
2. Build React against the real FastAPI (not mocks) in parallel, page by
   page, in the phase order in §14 below.
3. **Streamlit dashboard keeps running, untouched, for the entire buildout**
   — it remains the production UI until cutover.
4. Cutover criteria (all required, per your instruction): React has
   equivalent functionality for all 14 areas + passing frontend/backend
   tests + both approval workflows verified working + entity relationships
   verified working + auth verified working + a production validation
   period (manual CTO-usability pass, §6-style) with no regressions found.
5. Only after cutover: decommission the Streamlit Render service. (Not part
   of this plan's scope — a separate, later decision.)

---

## 14. Test Strategy

**Backend (pytest, extending the existing 1414-test suite, same
mongomock/fixture conventions already established in this repo):**
- Route tests per endpoint (status codes, response shape, auth required)
- Reused-service tests already exist (`app/query/*`, `app/ui/data.py`,
  `app/replies/approval.py`, `app/calendar/actions.py` are all already
  tested) — new tests only cover the thin route-handler glue, not
  re-testing business logic
- Auth tests (login success/failure, missing/expired/invalid cookie,
  `dashboard_read_only` enforcement server-side)
- Approval-workflow integration tests (approve → persisted correctly,
  reject → persisted correctly, same assertions pattern as
  `test_ui_smoke.py`'s existing approval tests, adapted to HTTP calls)

**Frontend (Vitest + React Testing Library):**
- Component tests (DataTable, StatusBadge, ApprovalCard, EmptyState, etc.)
- API integration tests (mocked fetch, verifying hooks call the right
  endpoint/params)
- Critical workflow tests (approve/reject/edit flows render correctly and
  call the right mutation)

**End-to-end (Playwright, new):**
- Login → Dashboard overview loads
- Email list → select → conversation view renders
- Opportunity detail loads with related entities
- Project field update (PATCH) persists and re-renders
- Reply approval: approve/reject/edit each work end-to-end
- Calendar approval: create/ignore each work end-to-end

**Non-negotiable:** the existing 1414 backend tests must remain passing
throughout — none of this plan's additions touch existing test files for
Emails/Threads/People/MCP/pipeline.

---

## 15. Risks

- **Auth is new from scratch** (nothing in the current `Settings` class to
  repurpose) — highest-effort, highest-stakes single piece; must be gotten
  right before any mutation endpoint ships.
- **Promoting `_person_label`/`_org_label`/recipient-fallback logic** out of
  `dashboard.py` into a shared layer needs care not to silently change
  behavior for the still-running Streamlit dashboard if that promotion
  changes the function's own module (mitigate: Streamlit imports the
  promoted function too, single source of truth, tested once).
- **Pagination is a genuinely new capability** on every repository — needs
  its own tests to avoid off-by-one/duplicate-row bugs across pages.
- **Calendar provider dependency injection** (`ProviderFactory.create_calendar_provider`
  is currently constructed inline in `dashboard.py`) needs a clean FastAPI
  `Depends()` equivalent — small but easy to get subtly wrong (e.g.
  constructing it even in read-only mode, wasting a real provider
  connection).
- **Running 3 Render services** (vs. 2 today) during the parallel period
  has a real, if modest, cost/ops overhead until Streamlit is decommissioned.

---

## 16. Estimated Implementation Phases

1. **FastAPI skeleton + auth** — app factory, session-cookie auth,
   `dashboard_read_only` enforcement, health check, OpenAPI docs enabled.
2. **Read-only GET endpoints**, all 14 areas, pagination/filtering added to
   repositories as needed — zero new business logic, pure route + service
   wiring.
3. **Mutation endpoints** — reply approval (3 routes), calendar approval (2
   routes), project/opportunity field updates (2 routes).
4. **React skeleton** — Vite+TS+Router+AppShell+Sidebar+TopBar, typed API
   client, React Query wired, auth/login flow.
5. **React read-only pages**, all 14 areas, DataTable/EntityDetail
   components, reusing the approved column orders.
6. **React mutation UI** — approval cards, project field editor, wired to
   the mutation endpoints.
7. **Polish pass** — loading/empty/error states, responsive layout check,
   the CTO-usability review from the prior pass applied to the new UI.
8. **Test completion** — backend route tests, frontend component/integration
   tests, E2E suite.
9. **Parallel-run validation** — both UIs live simultaneously, manual
   production validation against real/representative data.
10. **Cutover decision** — only after all of §13's criteria are met.

---

## 17. Files

### New files to be created (none exist yet)
- `app/api/main.py` — FastAPI app factory
- `app/api/dependencies.py` — auth dependency, db dependency, settings
- `app/api/routers/{emails,people,organizations,projects,opportunities,commitments,follow_ups,meetings,personal_items,threads,knowledge,reply_drafts,calendar_actions,auth}.py`
- `app/api/schemas/*.py` — request/response Pydantic models (where
  `app/query/schemas.py` doesn't already cover the shape)
- New `tests/test_api_*.py` files mirroring the router list above
- `frontend/` — entire new React+Vite+TS project (package.json, src/ tree per §5)
- `frontend/tests/*` — Vitest/RTL/Playwright suites

### Existing files needing modification
- `app/config/settings.py` — add `api_secret_key`, `api_session_ttl_minutes`
  (and `cors_allowed_origins` only if the split-service deployment is chosen
  instead of §12's recommendation)
- `app/database/repositories.py` — add optional `limit`/`offset` params to
  `_BaseRepository.find_many` (additive, backward-compatible default
  `offset=0`)
- `app/ui/data.py` — promote `_person_label`/`_org_label`
  (currently private to `dashboard.py`) here, or to a new shared module, so
  both Streamlit and FastAPI import the same function
- `Dockerfile` — add a React build stage / adjust `COPY`
- `README.md` — document the new service and local dev workflow
- `requirements.txt` — add `fastapi`, `python-jose`/`pyjwt` or equivalent,
  `passlib[bcrypt]` (uvicorn/starlette already present)

### Existing files that must remain untouched by this migration
- `app/mcp/server.py`, `app/mcp/tools.py` — MCP continues unchanged
- `app/entities/models.py`, `app/email/models.py` (Emails/Threads/People —
  **frozen**, confirmed no field changes anywhere in this plan)
- `app/pipeline.py`, `app/entities/resolution.py` — pipeline logic untouched
- `app/ui/dashboard.py` — kept running as-is until cutover (§13)
- All 3 existing migration scripts (`scripts/remove_email_id_record_id_date_fields.py`,
  `scripts/remove_thread_id_field.py`, `scripts/remove_person_source_note_link_fields.py`)
- The full existing test suite (1414 tests) — none modified, only added to
