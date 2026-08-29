# Detecting Agent (Level 3.2)

## Architecture

```
┌──────────────────────┐
│    Signal Repository │
└──────────┬───────────┘
           │
           ▼
   ┌───────────────┐
   │   Detecting   │
   │     Agent     │
   └───────┬───────┘
           │
   ┌───────┴────────┐
   │                │
   ▼                ▼
Operational        Product
signals            signals
   │                │
   └───────┬────────┘
           ▼
     Evidence Set
           │
           ├────→ Enterprise Knowledge
           │
           ▼
     Gemini / ADK
           │
           ▼
     DetectionResult
       │           │
       ▼           ▼
   Incident     Feature
  detection   opportunity
       │           │
       ▼           ▼
  Resolution     Review
  (future)      (future)
```

## Closed-loop vision (future, not built here)

```
Customer/User Signals
         ↓
   Detecting Agent
         ↓
  Feature Opportunity
         ↓
    Human Review
         ↓
       Ticket
         ↓
   Planning Agent
         ↓
   SDLC Pipeline


Cloud Monitoring/Logging
         ↓
   Monitoring Agent
         ↓
       Signals
         ↓
   Detecting Agent
         ↓
  Incident Detection
         ↓
Incident Resolution
         ↓
   Testing → Deployment
```

This level implements everything above the `DetectionResult` line only.
`IncidentCandidate`/`FeatureCandidate`, Incident Resolution, human review,
and ticket creation are all future work — explicitly out of scope here.

No legacy predecessor exists for Detecting under this name — read
`docs/architecture/signal_platform.md` and
`docs/architecture/monitoring_agent.md` first; this document only covers
what's new, not the Signal contract or persistence layout, both reused
unchanged.

## 1. Why Detecting exists

Every signal Monitoring (or a future product-feedback adapter) produces is
one isolated fact. No single Signal says "this is an incident" or "this is
a feature request" — that requires looking at several signals together,
reasoning about time, repetition, and independent corroboration. Detecting
is the layer that does that reasoning. Without it, "what is happening"
(Monitoring) never becomes "what should we do about it" — every signal
would sit in the repository unread until a human happened to notice a
pattern manually.

## 2. Monitoring vs Detecting

| | Monitoring (3.1) | Detecting (3.2) |
|---|---|---|
| Question | "What is happening?" | "What might these observations represent?" |
| Reasoning | None — deterministic Python only (see `docs/architecture/monitoring_agent.md` §3) | Genuine LLM reasoning over correlated evidence |
| Input | Live Cloud Monitoring/Logging API | Already-persisted Signals, via `SignalGateway` |
| Output | `Signal`s | `DetectionResult` |
| Calls the other? | Never calls Detecting directly | Never calls Monitoring directly |

Both write to/read from `SignalRepository` — that's the only connection
between them (§27 of the task): `Monitoring → SignalRepository →
Detecting`, never `Monitoring → Detecting`. This keeps both independently
usable and testable, exactly as every other agent pair in this codebase is.

## 3. Signal vs DetectionResult

```
Signal          = "what was observed"                       (Level 3, unchanged)
DetectionResult = "what Detecting's interpretation layer
                   believes a bounded set of Signals may represent"   (this level)
Candidate       = "a reviewable IncidentCandidate/FeatureCandidate"    (future)
Ticket          = "what the organization decided to act upon"         (existing)
```

`DetectionResult` (`app/domain/detection.py`) never embeds a `Signal`
object — only `supporting_signal_ids: list[str]` references, and those
references are verified (§11) before the object is ever constructed. A
`Signal` retrieved as evidence is never mutated, re-saved, or have its
`status` changed by Detecting — proven directly
(`test_original_signals_remain_unchanged_after_detection`).

## 4. Operational detection

`DetectingInput.domain = "operational"`. Default evidence types:
`METRIC_ANOMALY`, `LATENCY_ANOMALY`, `AVAILABILITY_DEGRADATION`,
`LOG_ERROR`, `APPLICATION_ERROR`, `DEPLOYMENT_EVENT`. Typical window:
minutes to a few hours (`window_minutes`, default 60). The model is
instructed to look for temporal relationships (did a deployment precede a
failure spike?) and asked to conclude `INCIDENT` only when the evidence
genuinely supports it — a lone, isolated signal is expected to produce
`NO_ACTION`, not a forced incident
(`test_isolated_low_severity_signal_can_result_in_no_action`).

## 5. Product detection

`DetectingInput.domain = "product"`. Default evidence types:
`CUSTOMER_FEEDBACK`, `SUPPORT_FEEDBACK`, `FEATURE_REQUEST_PATTERN`,
`USER_BEHAVIOR`, `ADOPTION_ANOMALY`. Typical window: days to weeks
(`window_minutes` can be set arbitrarily larger — see §13 for why this
needed its own, separate ceiling from Monitoring's). The model is
instructed to weigh independent sources (multiple customers, support
tickets, and a usage pattern converging on the same unmet need is much
stronger evidence than one data point) and conclude `FEATURE_OPPORTUNITY`
only when that convergence is real
(`test_repeated_customer_and_support_feedback_produces_feature_opportunity`,
`test_weak_single_feedback_signal_can_result_in_no_action`).

One `DetectingInput` model covers both paths — `domain` selects the
framing and default signal types; there is no second agent
(`DetectingAgent` handles both, `_DOMAIN_SIGNAL_TYPES` in
`app/agents/detecting.py` is the only branch point).

## 6. Signal retrieval

```
DetectingAgent
      ↓
SignalGateway
      ↓
SignalRepository
      ↓
Firestore / memory
```

`DetectingAgent._retrieve_evidence()` calls `context.signals.query()` (the
existing `SignalGateway`/`SignalRepository.query`, unchanged, unextended —
no new query operation was needed) once per relevant `SignalType`, merges
by `signal_id`, sorts newest-first, and truncates to
`min(DetectingInput.max_signals, settings.detecting_max_signals)`. Nothing
in `DetectingAgent` imports Firestore directly, and nothing bypasses
`SignalGateway` — verified structurally alongside the isolation tests
inherited from Level 3.

## 7. Correlation is not deduplication

Deduplication happens once, at Signal ingestion (`compute_fingerprint`,
`SignalRepository.find_by_fingerprint` — Level 3/3.1, untouched here).
Detecting operates strictly *after* that: it receives already-deduplicated
Signals and looks for relationships *between distinct* signals (a
deployment event, an error-rate signal, and a latency signal that are three
separate Signals but may describe one underlying event). No fingerprint
logic was modified, and no second fingerprint algorithm was created for
Signals — `DetectionResult` gets its own, entirely separate identity
mechanism (§14) for a different purpose (detection dedup, not signal
dedup).

## 8. Temporal reasoning

Every signal in the evidence set carries `observed_at` (ISO 8601,
timezone-aware). The instruction explicitly asks the model to reason about
temporal relationships (§20 of the task) — "did a deployment precede an
error spike," "do feedback signals cluster over time" — using the
timestamps already present in the evidence set. No time-series ML system
was built; this is structured evidence handed to an LLM that reasons over
it directly, exactly as the task specified.

## 9. Enterprise Knowledge

Reuses `query_enterprise_knowledge` and the existing `detecting_agent`
retrieval profile (`app/knowledge/policies/retrieval_policy.py` —
`OPERATIONS`, `TROUBLESHOOTING`, `INCIDENT` — already existed, added in an
earlier level; not modified here, since it already covers what Detecting
needs). The model decides when to call it — never automatic, never forced
for every run (§13/§22 of the task). Knowledge is explicitly, repeatedly
labeled in the instruction as **contextual grounding, not evidence**: the
model is told never to put a `document_id` into `supporting_signal_ids`,
and `DetectionResult.knowledge_references` is a *separate* field from
`supporting_signal_ids` — proven directly
(`test_knowledge_references_stored_separately_from_evidence`). Unlike
signal references, `knowledge_references` is **not** cross-checked against
a retrieval log (§14 of the task: Knowledge results → contextual grounding,
a softer standard than Signal IDs → evidence) — documented as an
intentional asymmetry, not an oversight.

## 10. ADK / Gemini integration

`app/agents/detecting.py::_detecting_llm_agent` — a real
`google.adk.agents.LlmAgent`, `model=settings.gemini_model` (same
configuration as every other agent), `output_schema=DetectionOutput`,
`tools=KNOWLEDGE_TOOLS` (exactly one tool: `query_enterprise_knowledge` —
verified, `test_no_arbitrary_tool_beyond_knowledge`),
`before_tool_callback=_tool_capability_gate`,
`after_model_callback=_track_usage_metrics` — the same two shared
callbacks reused by every prior agent, not reimplemented.

**No signal-fetching tool is exposed to the LLM at all.** Unlike knowledge
(an on-demand tool call), signal evidence retrieval happens entirely in
`_perform()`, in deterministic Python, *before* the LLM is ever invoked —
the evidence set is fixed and bounded by the time Gemini sees it, and there
is no tool call through which the model could expand it. This is a
deliberate design choice distinguishing Detecting from Monitoring
differently than either extreme: Monitoring has no LLM at all (§3 of
`monitoring_agent.md`); Detecting has genuine LLM reasoning, but only over
evidence the application already assembled — the model never controls its
own evidence acquisition.

## 11. Evidence-first validation — the critical property

This is the single most important mechanism in this agent, mirroring
`TestingAgent`'s ground-truth override one layer up (interpretation instead
of pass/fail, but the same discipline): the model's own claims are never
trusted at face value for the one field that matters most.

`DetectingAgent._validate_evidence()`:

1. Computes `valid_ids = {s.signal_id for s in evidence}` — the *actual*
   retrieved evidence set, not what the model says it saw.
2. Filters `DetectionOutput.supporting_signal_ids` down to only the ids
   that are actually in `valid_ids`. Any id the model invented is silently
   dropped and logged as a warning
   (`test_fabricated_signal_id_is_rejected_not_trusted`,
   `test_partially_fabricated_ids_keeps_only_real_ones`).
3. If, after filtering, a non-`NO_ACTION` detection has fewer than
   `_MIN_SUPPORTING_SIGNALS` (= 1) verified ids left, the **entire result**
   is downgraded to `NO_ACTION` with `confidence` forced to `0.0` —
   regardless of what confidence the model originally claimed. A
   fabricated-evidence `INCIDENT`/`FEATURE_OPPORTUNITY` can never reach
   persistence.

Before any of this, if retrieval itself returns **zero** Signals, the agent
never even calls Gemini — it deterministically returns `NO_ACTION` with
`confidence=0.0` and a rationale stating no evidence was found
(`test_no_signals_produces_deterministic_no_action_without_calling_llm`).
This is the strongest possible evidence-first guarantee for the empty case:
there's no model output to validate because there's no model call to make.

## 12. Confidence vs severity

Kept deliberately separate, matching the task's explicit instruction:
`confidence` (0.0–1.0) is the model's confidence in its *interpretation* of
the evidence, not a measure of how strong any individual Signal is;
`severity` reuses the existing `SignalSeverity` enum (`INFO`/`WARNING`/
`ERROR`/`CRITICAL` — no second severity vocabulary was introduced) and is
optional (`None` for most `FEATURE_OPPORTUNITY`/`NO_ACTION` results, since
severity is primarily an operational-incident concept). Evidence
integrity always wins over a high claimed confidence: a fabricated
"confidence=0.99" detection is still downgraded to `confidence=0.0` if its
evidence doesn't check out (§11) — confidence never overrides missing
evidence, exactly as the task required.

**Minimum evidence rule, documented explicitly** (§18 of the task):
`_MIN_SUPPORTING_SIGNALS = 1` — any `INCIDENT`/`FEATURE_OPPORTUNITY` needs
at least one *verified* (non-fabricated) supporting Signal. This is a
floor, not a quality bar — a single real Signal is still evidence; whether
it's *sufficient* to justify high confidence is exactly the judgment call
left to the model's `confidence` field and `rationale`, not something this
deterministic rule second-guesses further.

## 13. Persistence

`DetectionResult` is **not** stored as an `Artifact`. Decision, documented
directly in `app/domain/detection.py`'s module docstring:
`Artifact.parent_artifact_ids` models an SDLC stage-to-stage handoff
consumed by the *next* `QuipuAgent` in a chain (Plan → Architecture → Code
→ Test → Deploy); `DetectionResult` isn't consumed by another agent in this
level at all (Incident Resolution/Feature review, the eventual consumers,
are future work) — forcing it into `Artifact` would blur exactly the
evidence-vs-interpretation distinction Level 3/3.1/3.2 have consistently
protected.
Instead it gets the same treatment `Signal` did one level up: its own
narrow domain model (`app/domain/detection.py`) + repository
(`app/persistence/repositories/detection.py`,
`InMemoryDetectionRepository`, `FirestoreDetectionRepository`) + gateway
(`app/agent_runtime/gateways/detections.py`) — the identical pattern, not a
new one. Firestore layout: top-level `detections/{detection_id}` (not
workflow-scoped — same rationale as `signals/{signal_id}`: a
`DetectionResult` doesn't necessarily belong to any workflow either).

`detecting_max_window_minutes` (30 days) is deliberately a **separate,
larger** ceiling than Monitoring's `monitoring_max_window_minutes` (24h):
Monitoring bounds a *live Cloud API* query (hours is sensible for that);
Detecting reads already-persisted Signals, and product detection
legitimately needs weeks-scale windows (§4 of the task's own examples) —
reusing Monitoring's tighter ceiling would have made product detection
impossible. `detecting_max_signals` (default 50) is the separate,
LLM-context-facing ceiling (§26 — see next section).

## 14. Deduplication (of detections, not signals)

`app/domain/detection.py::compute_detection_fingerprint(detection_type,
subject, supporting_signal_ids, window_minutes)` — a deliberately separate
function from `Signal`'s `compute_fingerprint`, not a reuse of it (§7/§21
of the task: conflating the two would blur the evidence/interpretation
boundary this whole level protects). Same detection_type + subject +
signal set + window → same fingerprint, order-independent (signal ids are
sorted before hashing). `DetectingAgent._finalize()` checks
`DetectionGateway.find_by_fingerprint()` before saving; if a match exists,
it returns the existing `DetectionResult` instead of creating a duplicate
— proven directly (`test_repeated_run_over_same_evidence_does_not_duplicate`).
Not a distributed detection engine — the same "just the contract boundary"
scope `Signal`'s own dedup mechanism established.

## 15. Security

- **Bounded signal retrieval**: every `SignalGateway.query()` call carries
  an explicit `since`/`limit`; nothing retrieves an unbounded result set.
- **Bounded LLM context**: the evidence set handed to Gemini is capped at
  `min(DetectingInput.max_signals, settings.detecting_max_signals)` —
  proven directly with 20 and 60 seeded signals
  (`test_signal_retrieval_bounded_by_max_signals`,
  `test_max_signals_capped_by_configured_ceiling`) — a caller cannot widen
  past the configured ceiling even by requesting a huge `max_signals`.
- **No raw metadata forwarded**: `_signal_to_evidence_dict()` sends only
  typed fields plus the already-sanitized `evidence` dict — `Signal.
  metadata` (the free-form bucket) is never included, proven directly
  (`test_evidence_dict_does_not_include_raw_signal_metadata_field`).
  Sanitization itself is not redone here — it already happened once, at
  ingestion, via `app.signals.sanitize.sanitize_metadata` (Level 3);
  Detecting doesn't need a second pass, it inherits the guarantee.
- **No arbitrary query surface**: `DetectingInput` has no `filter`/`query`
  string field — only typed, closed-vocabulary fields
  (`test_detecting_input_has_no_raw_query_string_field`).
- **Environment scope reused, not duplicated**: `environment` is validated
  against `settings.cloud_run_allowed_environments` — the same allow-list
  Deployment/Monitoring already enforce.
- **No shell/subprocess anywhere** in `app/agents/detecting.py` (verified
  structurally).

## 16. Capability model

`DetectingAgent.capabilities = {READ_SIGNALS, QUERY_KNOWLEDGE,
WRITE_DETECTION}`. Two new, narrow capabilities were added to
`AgentCapability` (`app/agent_runtime/capabilities.py`), justified inline
there: `READ_SIGNALS` is distinct from `READ_MONITORING` (Monitoring's
capability to call the *live* Cloud Monitoring/Logging APIs) — Detecting
never touches those APIs, only the already-persisted `Signal` record, a
different concern deserving its own capability. `WRITE_DETECTION` mirrors
`WRITE_ARTIFACT`'s role for every other agent: permission to persist the
agent's own structured output/audit record, not a real-world mutation
capability — Detecting's read-only posture is preserved even though it
technically "writes" its own interpretation record.

Enforced in two layers (there is no ADK tool boundary layer for signal
retrieval, since retrieval isn't exposed as a tool — §10): **(1)** agent
entry — `self.require_capability(READ_SIGNALS)` in `_perform()`; **(2)**
implementation boundary — `_retrieve_evidence()` takes `granted:
set[AgentCapability]` as an **explicit parameter**, re-checked before
touching `SignalGateway`, the same pattern `MonitoringAgent` established
(`test_retrieve_evidence_rejects_when_capability_not_granted`). The ADK
tool boundary *does* apply to the one tool that exists
(`query_enterprise_knowledge`, gated by the shared
`_tool_capability_gate`/`_TOOL_CAPABILITY_MAP`, unchanged).

Explicitly **not** granted: `WRITE_CODE`, `DEPLOY`, `WRITE_JIRA`,
`RESOLVE_INCIDENT`, `ROLLBACK`, `CREATE_INCIDENT` — verified directly
(`test_detecting_agent_capabilities_are_read_only`,
`test_detecting_agent_capabilities_exclude_mutation_capabilities`).

## 17. No chain-of-thought storage

`DetectionResult.rationale` and `DetectionOutput.rationale` are both
instructed, in the prompt itself, to be "a concise, decision-relevant
explanation — not private step-by-step reasoning." Nothing in the schema
or persistence layer has a field for raw model reasoning traces, hidden
"thinking" tokens, or anything beyond the same handful of audit fields
`DetectionResult` exposes: `rationale`, `summary`, `supporting_signal_ids`,
`knowledge_references`, `confidence`, `detected_at`,
`observation_window_minutes`.

## 18. Standalone execution

`DetectingAgent` requires only `AgentInput` + `AgentContext` (with
`signals`/`detections`/`knowledge` populated) — no
`OrchestrationService` dependency anywhere in `_perform()`, matching the
pattern established for every prior agent. `DetectingAgent` is registered
in `app/orchestration/registry_setup.py` (resolvable via
`registry.get("detecting_agent")`, consistent discoverability with every
other agent) but is **not** added to `STAGE_ORDER`/`STAGE_TO_AGENT_ID` in
`app.orchestration.transitions` — it is not an SDLC stage, and the
happy-path `SequentialAgent`/`LoopAgent` were not touched. Reacting to a
`DetectionResult` from the orchestration layer is explicitly deferred.

## 19. Future Incident Resolution / Feature Candidate / human review

Not implemented here. `DetectionType.INCIDENT` is the signal a future
Incident Resolution Agent would consume; `DetectionType.FEATURE_OPPORTUNITY`
is what a future human-review step would see before it ever becomes a
`Ticket`. No `IncidentCandidate`/`FeatureCandidate` model, no automatic
ticket/Jira creation, and no remediation/rollback logic exists anywhere in
this level — `DetectionResult` is the final artifact this level produces.

## 20. Google services

| Google Service | Quipu Role | Used by DetectingAgent? |
|---|---|---|
| Google ADK | Agent runtime | Yes — `_detecting_llm_agent` |
| Gemini | Interpretation reasoning | Yes — the genuine reasoning task this agent exists for |
| Agent Search | Enterprise knowledge | Yes, on-demand via the existing `query_enterprise_knowledge` tool and `detecting_agent` retrieval profile (both already existed) |
| Firestore | Durable state | Yes — `FirestoreDetectionRepository`, reused pattern |
| Cloud Monitoring / Cloud Logging | Production telemetry | No — Detecting never calls these directly; it only reads what Monitoring already normalized into `Signal`s |
| Pub/Sub | — | Not introduced (explicitly out of scope) |

## 21. Testing

`tests/test_detection_domain.py` (27), `tests/test_detection_persistence.py`
(18), `tests/test_detecting_agent.py` (41) — 86 new tests covering: domain
validation, fingerprint determinism/distinctness, repository contract
(save/get/find_by_fingerprint/query, Firestore serialization round-trip),
lifecycle, input validation, the zero-evidence deterministic-`NO_ACTION`
path, operational scenarios (deployment + error spike, isolated low-
severity signal, unrelated signals), product scenarios (repeated
feedback → opportunity, weak single signal, user-behavior pattern),
**adversarial evidence tests** (fully fabricated signal id, partially
fabricated, a real-but-weak single signal, an incident claim over zero
retrieved signals), knowledge-vs-evidence separation, LLM failure/empty/
malformed/invalid-confidence/invalid-type handling, deduplication (repeat
run over identical evidence), capability enforcement at both layers, and
security (bounded retrieval, bounded LLM context, no raw metadata leakage,
no query-string injection surface). No test requires live Gemini/Google
credentials.

Full suite: **607 passed, 6 skipped** (pre-existing gated integration
tests) — no regressions.
