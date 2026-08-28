# Enterprise Knowledge Platform
# (1.3A contracts + 1.3B-1 local service + 1.3B-2 Google retrieval adapter)

Level 1.3A defined the typed contracts. Level 1.3B-1 added a real, local
retrieval *pipeline* behind them — `LocalKnowledgeService` — with
`InMemoryRetrievalBackend` as the only retrieval engine (deterministic
keyword matching, purely to validate the pipeline). Level 1.3B-2 adds the
first real Google Cloud integration: `GoogleSearchRetrievalBackend`, a second
`RetrievalBackend` implementation. No other layer changed to support it.

```
Agent
  |
KnowledgeGateway        (app/agent_runtime — agent-facing, unchanged)
  |
KnowledgeService         (app/knowledge/service.py — orchestrates the pipeline)
  |
RetrievalBackend          (app/knowledge/backend.py — pluggable retrieval engine)
  |
  +-- InMemoryRetrievalBackend    (app/knowledge/backends/in_memory.py — 1.3B-1)
  +-- GoogleSearchRetrievalBackend (app/knowledge/backends/google_search.py — 1.3B-2)
```

## Production Retrieval

```
Quipu KnowledgeService
        |
Google Retrieval Backend        (GoogleSearchRetrievalBackend)
        |
Google Enterprise Search / Agent Search   (Discovery Engine API)
        |
Enterprise Knowledge
```

### 1. What Google service is actually being used

**Agent Search** — the current (verified 2026-08) name for what was
previously Vertex AI Search / Vertex AI Search and Conversation / Generative
AI App Builder / "Enterprise Search". The underlying **Discovery Engine
API** and its Python client, **`google-cloud-discoveryengine`**
(`google.cloud.discoveryengine_v1`), are unchanged by the rename — Google's
own docs describe the naming shift while noting existing customers need no
migration. Vertex AI itself was separately rebranded to the **Gemini
Enterprise Agent Platform** at Google Cloud Next 2026; Agent Search sits
under that umbrella but is consumed here as a standalone Discovery Engine
API client, independent of Gemini/ADK.

`GoogleSearchRetrievalBackend` uses `SearchServiceAsyncClient.search()`,
configured with `content_search_spec.search_result_mode = CHUNKS` so
results come back as `SearchResult.chunk` objects — verified directly
against the installed SDK's proto field names (not blog posts): `Chunk` has
`id`, `content`, `relevance_score`, and `document_metadata.uri` /
`document_metadata.struct_data`, which map almost directly onto
`RetrievalResult`.

### 2. What Google handles vs. what Quipu handles

| | Google (Agent Search) | Quipu |
|---|---|---|
| Query execution | yes — semantic + keyword retrieval over the datastore | no |
| Hard filtering (`knowledge_type`, `domain`, `technology`, `service`, `environment`, custom metadata) | yes, via `SearchRequest.filter` — **if** the datastore schema indexes those fields as filterable | requests it (builds the filter string); cannot verify the schema actually honors it |
| Result ordering (Google's own relevance) | yes — the score on `Chunk.relevance_score` | consumed as the `RELEVANCE_WEIGHT` input to enterprise ranking, not treated as final |
| Authority/freshness/context ranking | no equivalent concept | yes — `apply_enterprise_ranking()`, unchanged from 1.3B-1 |
| Validity window (`effective_from`/`effective_until`) | not attempted | **not implemented for Google-backed results** — see limitations |
| Provenance (source URI, chunk id) | yes — `document_metadata.uri`, `chunk.id`, `chunk.name` | preserved into `RetrievalResult`, never discarded |

### 3. How metadata filters are mapped

`_build_filter()` builds a Discovery Engine filter expression of the form
`field: ANY("value")` clauses joined with `AND`, combining `knowledge_type`
plus `RetrievalPolicy.metadata_filters` plus `KnowledgeRequest.filters`.
`domain`/`technology`/`service`/`environment` get no special treatment
beyond being included by name — like every other filter key, this assumes
the target datastore's schema indexes it as a filterable structured field.
**This is a deployment-time assumption the adapter cannot verify** — if a
field isn't indexed as filterable, Google will reject the request
(surfaces as `GoogleInvalidRequestError`) rather than silently ignoring it.

### 4. How provenance is preserved

`chunk.id` -> `RetrievalResult.chunk_id`, `document_metadata.uri` (falling
back to `chunk.name`, the fully-qualified Discovery Engine resource path) ->
`RetrievalResult.source`, and the chunk's full `document_metadata.struct_data`
is copied into `RetrievalResult.metadata` alongside `google_chunk_name` for
traceability back to the exact Google resource. Nothing is invented: where
Google has no equivalent for a Quipu field (see below), the adapter uses a
documented, conservative default instead of fabricating a value.

### 5. How ranking is split between Google and Quipu

Google's `Chunk.relevance_score` becomes the *input* relevance signal;
`apply_enterprise_ranking()` (1.3B-1, unmodified) is still the only place
final ranking happens — no second, competing ranking system was created.
The backend requests `page_size = min(50, max(top_k, max_context_items) *
2)`, deliberately oversampling so Quipu's ranking layer has real headroom
to reorder rather than just rubber-stamping Google's order.

### 6. Authentication

Standard Google Cloud **Application Default Credentials (ADC)** —
`SearchServiceAsyncClient()` is constructed with no explicit credentials
argument, exactly as Google's own current samples do. Locally:
`gcloud auth application-default login`. In deployment: a service account
or Workload Identity, outside this repo entirely. No key file path, no
embedded secret, no custom auth code — `GOOGLE_APPLICATION_CREDENTIALS` in
`.env.example` (already present from earlier levels) is the one ADC-recognized
override, left empty by default.

### 7. Local development / testing approach

Every unit test (`tests/test_google_search_backend.py`) injects a fake
client satisfying `search(request, timeout=...) -> async-iterable-of-results`
— no real SDK call, no credentials, no network, ever, in the normal test
run. A separate, clearly isolated integration test
(`tests/integration/test_google_search_integration.py`) exists for anyone
with a real datastore, gated behind `QUIPU_RUN_GOOGLE_INTEGRATION_TESTS=true`;
`pytest tests/` alone never triggers it (it shows as `skipped`).

### 8. Why the Google implementation is isolated behind RetrievalBackend

`app/knowledge/backends/google_search.py` is the **only** file in the
repository allowed to import `google.cloud.discoveryengine_v1`. It is not
imported by `app/knowledge/__init__.py`, so `import app.knowledge` never
pulls in the Google SDK — verified directly (`test`-time assertion that
`google.cloud.discoveryengine_v1` isn't in `sys.modules` after importing
`app.knowledge`). `KnowledgeGateway`, `LocalKnowledgeService`, and every
agent depend only on the `RetrievalBackend` protocol; swapping
`InMemoryRetrievalBackend` for `GoogleSearchRetrievalBackend` is a one-line
change at whichever call site constructs `LocalKnowledgeService`, with zero
changes anywhere else.

### Known Google API limitations (verified against the SDK, not guessed)

- **No authority concept.** Google has nothing resembling
  `KnowledgeAuthority`. Every result defaults to `UNVERIFIED` unless the
  datastore's custom schema happens to expose `struct_data['authority_level']`
  matching Quipu's enum — a real value, never fabricated.
- **No validity-window enforcement.** `InMemoryRetrievalBackend` can check
  `effective_from`/`effective_until` because it owns the full typed
  `KnowledgeDocument`; `GoogleSearchRetrievalBackend` only sees whatever the
  datastore schema returns, so this isn't attempted here at all — per the
  task's own instruction to keep such policy logic out of the Google
  adapter rather than have it guess at schema shape. If validity enforcement
  against Google-backed results is needed later, it belongs as a
  backend-agnostic step in `KnowledgeService`, not duplicated per-backend.
- **No per-request retrieval-strategy toggle.** The base Discovery Engine
  Search API (verified against the installed SDK's `SearchRequest` fields)
  exposes no simple SEMANTIC-vs-KEYWORD switch for this use case — Agent
  Search's retrieval is inherently a blended approach. Quipu's
  `RetrievalStrategy` isn't mapped per-request; a service wrapping this
  backend should be constructed with `retrieval_strategy=RetrievalStrategy.HYBRID`
  to describe that honestly.
- **`relevance_score` may be absent** for some datastore/query
  configurations; the adapter falls back to a documented neutral `0.5`
  rather than inventing a fake precise value.
- **Filter schema is an assumption.** The adapter cannot inspect or verify
  the target datastore's indexing configuration; filter keys that aren't
  indexed as filterable will cause Google to reject the request.

## KnowledgeDocument vs KnowledgeChunk

A `KnowledgeDocument` is the canonical source (a policy, a wiki page, an
architecture doc) with authority, ownership, validity window and
confidentiality. It is never retrieved directly — a `KnowledgeChunk` is.
Every chunk carries its parent `document_id`, which is the root of provenance:
`RetrievalResult -> KnowledgeChunk -> KnowledgeDocument -> source`.

## KnowledgeRequest vs KnowledgeQuery

Unchanged distinction from Level 1.1, now made explicit: `KnowledgeRequest`
(`app/domain`) is what an agent asks for. `KnowledgeQuery` (`app/domain`) is
what the Knowledge Service actually executed — an audit record. Level 1.3A
extends `KnowledgeQuery` with `agent_name`, `workflow_id`, `filters`,
`retrieval_strategy`, `top_k` (all optional, so existing callers that only set
`text`/`knowledge_type`/`result_count` are unaffected).

`KnowledgeRequest` itself was **not** changed. The task asked for repository/
service/technology/environment context and multiple knowledge types on the
request; both are already expressible without new fields:

- Contextual constraints go in the existing `filters: dict[str, Any]` (e.g.
  `{"technology": "spring-boot", "environment": "production"}`) — adding
  typed fields for an open-ended, org-specific set of dimensions would be
  over-engineering for a contract this early.
- "Multiple knowledge types" belongs to the *policy* level, not one request:
  a `RetrievalPolicy.allowed_knowledge_types` scopes what an agent may ask
  for across many requests; a single `KnowledgeRequest.knowledge_type`
  stays singular, describing one ask.
- "Retrieval policy" isn't a field on the request either — the platform
  resolves it server-side from `agent_name` (see Agent retrieval profiles
  below), consistent with "agents request knowledge; the platform performs
  retrieval."

## KnowledgeGateway vs KnowledgeService vs RetrievalBackend

Three distinct layers, each replaceable independently:

- **`KnowledgeGateway`** (`app/agent_runtime`, unchanged since Level 1.2):
  `search(request) -> list[KnowledgeItem]`. Agent-facing, deliberately thin —
  agents never see chunk IDs, ranking internals, or retrieval strategy.
- **`KnowledgeService`** (`app/knowledge/service.py`): `search(request) ->
  KnowledgeContext`. Still a `Protocol`, unchanged from Level 1.3A, so
  existing `isinstance(x, KnowledgeService)` structural-typing checks keep
  working. `LocalKnowledgeService` is the concrete implementation added in
  1.3B-1 — named separately from the Protocol (not literally
  `KnowledgeService(retrieval_backend=...)` as the task's pseudocode showed)
  specifically so the Protocol export doesn't change; `LocalKnowledgeService`
  still satisfies it structurally via duck typing, no inheritance needed.
  Owns: policy resolution, ranking, result limits, context assembly, and an
  in-memory `audit_log: list[KnowledgeQuery]`.
- **`RetrievalBackend`** (`app/knowledge/backend.py`): `search(request,
  policy) -> list[RetrievalResult]`. The actual retrieval engine.
  `KnowledgeService` depends on this abstraction, never a concrete engine —
  that's what makes the Google Agent Search adapter a drop-in replacement
  later, with zero changes to `KnowledgeService` or any agent-facing code.

A concrete `KnowledgeGateway` (future work) would call a `KnowledgeService`
and narrow its `KnowledgeContext` down via
`app.knowledge.gateway_adapter.knowledge_context_to_items()`.

## Why the backend is abstracted, and why in-memory first

`RetrievalBackend` exists so `KnowledgeService`'s orchestration (policy
resolution, ranking, limits, audit) is provable and testable without any
real search engine. `InMemoryRetrievalBackend` is a deterministic,
in-process keyword matcher over a `list[KnowledgeDocument]` +
`list[KnowledgeChunk]` given at construction — explicitly *not*
production-quality search, only a fixture the contract tests run against.
When the Google Agent Search adapter arrives, it implements the same
`RetrievalBackend.search()` signature and plugs into the same
`KnowledgeService` unchanged.

## Filtering vs ranking

Two separate mechanisms, never blended into one score:

- **Filtering (hard, excludes)** happens in `RetrievalBackend`, since it's
  the only layer with the full `KnowledgeDocument` to check against:
  `knowledge_type`, `domain`/`technology`/`service`/`environment` (typed
  document fields), any other key in `KnowledgeRequest.filters` /
  `RetrievalPolicy.metadata_filters` (matched against `document.metadata`),
  and the validity window (`effective_from`/`effective_until`, checked with
  timezone-aware `datetime.now(timezone.utc)` — never a naive comparison,
  gated by `RetrievalPolicy.enforce_validity`). `KnowledgeService` computes
  *what* to filter by (merging request + policy filters); the backend
  applies it. A document failing a hard filter never appears in results —
  it is not merely down-ranked.
- **Ranking (soft, reorders)** happens in `KnowledgeService` via
  `app/knowledge/ranking.py`, after the backend returns already-filtered
  candidates and after `RetrievalPolicy.min_relevance_score` is applied as a
  floor. See "Ranking formula" below.

Confidentiality filtering is intentionally not implemented: there is no
requester-clearance concept anywhere in the contracts yet (no field on
`AgentInput`/`KnowledgeRequest` says what an agent/user is cleared to see),
so there's nothing to compare `KnowledgeDocument.confidentiality` against.
Deferred, per Level 1.3A's explicit "do not implement access control logic
yet" and unchanged by this level.

## Ranking formula

Deterministic, explainable, and intentionally simple — not a reranker.
Weights live in `app/knowledge/ranking.py` as named constants, not scattered
literals:

```
raw_score = relevance_score              (backend score, weight 1.0)
          + AUTHORITY_BONUS[authority_level]      (+0.15 .. -0.05)
          + FRESHNESS_BONUS (0.08)      if this is the freshest candidate
                                          AND policy.freshness_preference != NONE
          + TECHNOLOGY_MATCH_BONUS (0.05)  if "technology" was a filter in effect
          + SERVICE_MATCH_BONUS (0.05)     if "service" was a filter in effect
          + ENVIRONMENT_MATCH_BONUS (0.05) if "environment" was a filter in effect
```

Every bonus is small relative to the 0..1 relevance score, so authority/
freshness/context can nudge ordering but never override relevance outright.
`raw_score` (unclamped) is the sort key; the value actually stored in
`RetrievalResult.relevance_score` is `max(0, min(1, raw_score))` — clamped
separately from the sort key so two results tied at the relevance ceiling
(both scoring a perfect keyword match, say) can still be told apart in rank
order without the displayed score exceeding its documented `[0, 1]` range.

The technology/service/environment bonuses are computed from whichever
filters were actually in effect for the request — since results reaching
ranking already passed those as hard filters, the bonus is currently
uniform across survivors for a single query; it's kept as an explicit,
separate term (rather than folded into filtering) so a future policy that
treats one of these dimensions as a soft preference instead of a hard
requirement doesn't require restructuring the scorer.

Swap `app/knowledge/ranking.py` (or the injected `RetrievalBackend`) to
change ranking behaviour without touching `KnowledgeService`'s orchestration.

## Provenance

`chunk_id`, `document_id`, `source`, `knowledge_type`, `authority_level`
survive unchanged from `InMemoryRetrievalBackend`'s output through
`apply_enterprise_ranking()` (which copies every `RetrievalResult` via
`model_copy`, replacing only `relevance_score` and `rank`) into
`KnowledgeContext.results`. No filtering or ranking step discards them.

## Agent retrieval profiles

`app/knowledge/policies/retrieval_policy.py::AGENT_RETRIEVAL_PROFILES`
(Level 1.3A) is reused as-is, not duplicated. `LocalKnowledgeService`
resolves a policy via `get_retrieval_policy(request.agent_name)`; an unknown
agent name falls back to `DEFAULT_RETRIEVAL_POLICY` (all knowledge types
allowed). Profiles remain defaults, not hard restrictions — nothing in this
level blocks an agent from requesting an out-of-profile knowledge type.

## Why retrieval is still deferred

`InMemoryRetrievalBackend` proves the pipeline end-to-end (filtering,
ranking, limits, provenance, audit) with zero external dependencies. No
embeddings, vector index, real keyword index, reranker, or Google Cloud
service exists yet — those live behind `RetrievalBackend`, which is exactly
what makes adding the Google Agent Search adapter later a new
implementation of one small interface, not a rewrite of `KnowledgeService`.
