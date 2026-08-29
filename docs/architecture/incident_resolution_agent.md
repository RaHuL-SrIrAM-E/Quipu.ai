# Incident Resolution Agent (Level 3.3)

## Architecture

```
┌───────────────────────┐
│    DetectionResult    │
└───────────┬───────────┘
            │
            ▼
    Supporting Signals
            │
            ▼
┌───────────────────────┐
│ Incident Resolution   │
│        Agent          │
└───────────┬───────────┘
            │
   ┌────────┴─────────┐
   │                  │
   ▼                  ▼
Enterprise Knowledge   Artifacts
   │                  │
   └────────┬─────────┘
            ▼
      Gemini / ADK
            │
            ▼
      ResolutionResult
            │
   ┌────────┼──────────┐
   ▼        ▼          ▼
Code Fix  Rollback   Escalate
   │        │          │
   ▼        ▼          ▼
Codegen  Deployment   Human
   │
   ▼
Testing
   │
   ▼
Deployment
   │
   ▼
Monitoring
   │
   ▼
Verification
```

No legacy predecessor exists for this agent under this name (the old
`app/agents/incident_management.py` is a separate, untouched, pre-QuipuAgent
stub from the legacy architecture — see `docs/architecture/monitoring_agent.md`'s
note on the similar situation there). Read
`docs/architecture/detecting_agent.md` first — this document only covers
what's new one level further downstream.

## 1. Why Incident Resolution exists

A `DetectionResult` says "this looks like an incident" with a confidence
score and some evidence — it does not say *why*, what to *do* about it, or
*who* should do it. Without this agent, every incident detection would sit
unread until a human manually diagnosed root cause and picked a fix.
Incident Resolution is the layer that turns "this is probably an incident"
into a concrete, safety-checked recommendation an existing agent (or a
human) can act on.

## 2. Detecting vs Incident Resolution

| | Detecting (3.2) | Incident Resolution (3.3) |
|---|---|---|
| Question | "What might these observations represent?" | "Why is this probably happening, and what should be done?" |
| Input | Bounded Signal query criteria | One validated, `INCIDENT`-typed `DetectionResult` |
| Output | `DetectionResult` (type + confidence) | `ResolutionResult` (root cause + remediation plan) |
| Scope | Both operational and product domains | Operational incidents only — `FEATURE_OPPORTUNITY` detections are rejected outright, before Gemini is ever called |

Both are DETECT/INTERPRET layers, never EXECUTE layers — Incident
Resolution extends that discipline one step further: DETECT → DIAGNOSE →
DECIDE, still never EXECUTE. It never modifies files, runs shell commands,
deploys, rolls back, creates a Jira issue, calls a Cloud Run mutation API,
runs tests, or resolves the incident itself. It recommends; existing
specialist agents (`codegen_agent`/`testing_agent`/`deployment_agent`/
`architecture_agent`) remain responsible for execution, and a future
`OrchestrationService` extension is what will actually route to them.

## 3. Evidence flow

```
DetectionRepository
      ↓
DetectionResult
      ↓
supporting_signal_ids
      ↓
SignalGateway.get(signal_id) for each id
      ↓
actual Signals (only the ones that still resolve)
      ↓
ArtifactGateway.get(workflow_id, deployment_artifact_id) for correlated artifacts
      ↓
Resolution reasoning (Gemini)
```

`ResolutionInput` (parsed from `AgentInput.context`, the same extension
point every prior agent uses) has exactly one field: `detection_id`. The
task is explicit that "the primary input must be a validated
`DetectionResult`... do NOT accept arbitrary model-generated incident
descriptions as the authoritative input" — so there is no field through
which a caller could hand the agent a hand-assembled evidence bag; every
piece of context is resolved deterministically from the referenced
`DetectionResult`.

`IncidentResolutionAgent._perform()` rejects, before Gemini is ever
invoked:

- the `detection_id` doesn't resolve at all → `DETECTION_NOT_FOUND`
- the `DetectionResult.detection_type` isn't `INCIDENT` →
  `DETECTION_NOT_AN_INCIDENT` (proven directly,
  `test_feature_opportunity_rejected_before_gemini` — a
  `FEATURE_OPPORTUNITY` is never reinterpreted as an incident)
- none of `supporting_signal_ids` resolves to a real `Signal` →
  a **deterministic** `ESCALATE` result, no Gemini call at all (mirrors
  `DetectingAgent`'s zero-evidence path — "fail safely / escalate," never
  invent missing evidence)

If some (not all) supporting signals fail to resolve, the agent proceeds
with whatever did resolve — documented, not silently pretending the
missing ones don't matter (`test_partial_missing_signals_still_proceeds_with_resolved_ones`).

## 4. Diagnosis

`ResolutionProposal` (Gemini's structured output) carries
`diagnosis_summary`, `probable_root_cause`, `root_cause_confidence`, and an
optional `root_cause_candidates` list for when the model isn't certain
between a few plausible causes (§22 of the task — "do not force
certainty"). The instruction gives the model the full evidence set with
timestamps and asks it to reason about temporal ordering (did a deployment
precede the failure spike?), independent corroboration, and which of the
closed `RemediationStrategy` values actually fits — never free-form.

## 5. Enterprise Knowledge

Reuses `query_enterprise_knowledge` and the existing
`incident_resolution_agent` retrieval profile
(`app/knowledge/policies/retrieval_policy.py` — `INCIDENT`,
`TROUBLESHOOTING`, `OPERATIONS`, `ARCHITECTURE_PATTERN`,
`DEPLOYMENT_STANDARD` — already existed, added in an earlier level, not
modified here). On-demand only, never forced. Same asymmetry Detecting
established (`docs/architecture/detecting_agent.md` §9): knowledge is
**contextual grounding, not evidence** — the instruction explicitly tells
the model never to put a `document_id` into `supporting_signal_ids`/
`supporting_artifact_ids`, and `ResolutionResult.knowledge_references` is a
structurally separate field, not cross-checked against a retrieval log
(`test_knowledge_references_kept_separate_from_evidence`).

## 6. Gemini / ADK role

`app/agents/incident_resolution.py::_incident_resolution_llm_agent` — a
real `google.adk.agents.LlmAgent`, `model=settings.gemini_model`,
`output_schema=ResolutionProposal`, `tools=KNOWLEDGE_TOOLS` (exactly one
tool — verified, `test_no_arbitrary_tool_beyond_knowledge`),
`before_tool_callback=_tool_capability_gate`,
`after_model_callback=_track_usage_metrics` — the same shared callbacks
reused by every prior agent. Unlike Monitoring (no LLM at all) but like
Detecting, this agent has a genuine reasoning task: correlating evidence,
weighing root-cause candidates, and picking a remediation strategy is not
mechanical translation.

## 7. ResolutionResult

`app/domain/resolution.py::ResolutionResult` — framework-independent, no
Google SDK imports (verified structurally,
`test_domain_resolution_module_has_no_google_imports`):

```python
class ResolutionResult(BaseModel):
    resolution_id: str
    detection_id: str                      # lineage — never rewrites the DetectionResult itself

    diagnosis_summary: str
    probable_root_cause: str
    root_cause_confidence: float           # confidence in the DIAGNOSIS
    root_cause_candidates: list[str]

    remediation_strategy: RemediationStrategy
    remediation_rationale: str
    expected_outcome: str
    verification_strategy: str

    risk: RemediationRisk                  # risk of the RECOMMENDED remediation, not incident severity
    severity: SignalSeverity | None        # reuses Signal's severity vocabulary
    escalation_recommended: bool

    target_agent: str | None               # deterministically derived, never the model's own claim
    rollback_target: str | None

    supporting_signal_ids: list[str]       # evidence-first validated
    supporting_artifact_ids: list[str]     # evidence-first validated
    knowledge_references: list[str]        # contextual grounding, not evidence

    resolved_at: datetime
    fingerprint: str
```

Deliberately smaller than the task's example field list — no separate
`incident_id` (redundant with `detection_id`, since a `DetectionResult` of
type `INCIDENT` *is* the incident record); no free-text `risk` (closed
enum, §14); no raw chain-of-thought field at all (§37/§17).

## 8. Remediation strategies

`RemediationStrategy` (closed enum, `app/domain/enums.py`): `CODE_FIX`,
`RETEST`, `ARCHITECTURE_REVIEW`, `ROLLBACK`, `ESCALATE`, `NO_ACTION`. **No
`CONFIGURATION_CHANGE` value exists** — the task is explicit that
configuration mutation must not be invented without a safe execution path,
and none exists anywhere in Quipu today; that outcome is represented as
`ESCALATE` instead (verified,
`test_configuration_change_is_not_a_valid_strategy` — the string is
rejected outright by pydantic, there's no code path that could accept it).
An invalid value like `"execute_shell"` is rejected at the same schema
boundary (`test_invalid_strategy_rejected_at_schema_level`) — a closed enum
makes this a structural guarantee, not a runtime check that could be
missed.

## 9. Target-agent authorization

**The model's own `target_agent` claim is never trusted directly.**
`_STRATEGY_TARGET_AGENT` (`app/agents/incident_resolution.py`) is a fixed,
hardcoded map from the already-validated `RemediationStrategy` enum to an
allow-listed agent id string:

| Strategy | Target agent |
|---|---|
| `CODE_FIX` | `codegen_agent` |
| `RETEST` | `testing_agent` |
| `ARCHITECTURE_REVIEW` | `architecture_agent` |
| `ROLLBACK` | `deployment_agent` (recommendation only — see §11) |
| `ESCALATE` | `None` (human) |
| `NO_ACTION` | `None` |

`ResolutionResult.target_agent` is **always** the map lookup, never
`ResolutionProposal.target_agent` directly — proven directly with an
adversarial payload
(`test_arbitrary_target_agent_claim_is_never_trusted`: the model claims
`target_agent="malicious_agent"`, the persisted result still has
`target_agent="codegen_agent"`, because that's what `CODE_FIX` maps to).
This closes the "invent an arbitrary target" attack surface structurally
— there is no code path where a string the model wrote ends up as the
authoritative `target_agent`, not even after an allow-list check. The
model's own `target_agent` field still exists on `ResolutionProposal` (so
the model can express its reasoning/self-check), it's just never load-
bearing.

## 10. Risk policy — the deterministic safety layer

Mirrors `app/orchestration/decisions.py` turning a `ProposedDecision` into
a `Decision` only after transition-policy validation: here,
`_apply_safety_policy()` turns a `ResolutionProposal` into what's actually
persisted, and every rule can only ever **downgrade toward ESCALATE** —
never upgrade or invent a more permissive outcome than the model itself
proposed. In order:

1. **Evidence floor**: fewer than 1 verified `supporting_signal_id`
   survives validation → `ESCALATE`, `root_cause_confidence` forced to
   `0.0` (`test_fully_fabricated_signal_ids_forces_escalation`,
   `test_high_confidence_cannot_bypass_missing_evidence` — a claimed
   `confidence=0.99` over fabricated evidence still escalates).
2. **High remediation risk is never auto-authorized** (§14 of the task:
   "Do not allow the LLM to bypass deterministic high-risk rules") —
   `risk == HIGH` forces `ESCALATE` regardless of confidence
   (`test_high_risk_forces_escalation_even_with_strong_evidence`).
3. **Low root-cause confidence never authorizes automatic remediation** —
   `root_cause_confidence < settings.incident_resolution_min_confidence_for_auto_remediation`
   (default `0.7`) forces `ESCALATE`. This is exactly the task's own §21
   example: `severity=HIGH, confidence=0.62, risk=LOW` still escalates
   (`test_low_confidence_forces_escalation_despite_high_severity`) —
   confidence is checked independently of severity and risk, never
   conflated.
4. **`ROLLBACK` without a concrete `rollback_target`** forces `ESCALATE`
   (§31 adversarial E — `test_rollback_without_target_forces_escalation`):
   you cannot recommend a rollback without saying what to roll back to.
5. **`CODE_FIX` without application-level evidence** forces `ESCALATE`
   (§31 adversarial F): at least one *verified* supporting signal must be
   `APPLICATION_ERROR` or `LOG_ERROR` — a deployment-event or availability
   signal alone is not sufficient grounds to authorize a code change
   (`test_code_fix_without_code_related_evidence_forces_escalation`).

Severity, confidence, and risk are three genuinely independent fields
throughout — `ResolutionResult` never derives one from another, and the
safety policy checks each separately (§19/§21 of the task).

## 11. Rollback boundary

`DeploymentAgent` (Level 2.1) already established this boundary —
`rollback_strategy`/rollback metadata is structured, never executed.
Incident Resolution respects it exactly: `remediation_strategy=ROLLBACK`
plus `rollback_target` (the specific revision/service to roll back to) is
the entire output. No rollback execution code exists anywhere in this
agent, and `target_agent` for `ROLLBACK` is `deployment_agent` — a
*recommendation* that a future orchestrator could route to
`DeploymentAgent`, which itself still has no rollback-execution tool
either (unchanged from Level 2.1).

## 12. Codegen/Testing/Deployment relationship

Incident Resolution never invokes another agent directly — no
`CodegenAgent()`/`TestingAgent()`/`DeploymentAgent()` construction or call
exists anywhere in `app/agents/incident_resolution.py`. It produces a
`ResolutionResult` whose `target_agent` names one of these agents (or
`None`); routing to that agent is explicitly deferred to a future
`OrchestrationService` extension (§35 of the task — "the orchestrator
remains the ONLY component that routes agents").

## 13. Evidence validation

`_validate_evidence()` mirrors `DetectingAgent._validate_evidence`,
extended to cover both evidence types: `supporting_signal_ids` filtered
against the actually-retrieved `Signal` set, `supporting_artifact_ids`
filtered against the actually-retrieved deployment-artifact evidence.
Anything not in those sets is silently dropped and logged as a warning —
proven directly for both
(`test_fabricated_signal_id_dropped_not_trusted`,
`test_fabricated_artifact_id_dropped`).

## 14. Persistence

`ResolutionResult` is **not** stored as an `Artifact` — same rationale
`DetectionResult` already established
(`docs/architecture/detecting_agent.md` §13): it isn't an SDLC stage's
completed output consumed by the next agent in the
Plan→Architecture→Code→Test→Deploy chain. It gets the identical treatment:
its own narrow domain model (`app/domain/resolution.py`) + repository
(`app/persistence/repositories/resolution.py`,
`InMemoryResolutionRepository`, `FirestoreResolutionRepository`) + gateway
(`app/agent_runtime/gateways/resolutions.py`) — same in-memory/Firestore
pattern as every other repository in this codebase, no new persistence
technology. Firestore layout: top-level `resolutions/{resolution_id}` (not
workflow-scoped, same rationale as `signals/`/`detections/`).

## 15. Idempotency / deduplication

`app/domain/resolution.py::compute_resolution_fingerprint(detection_id,
remediation_strategy, subject)` — a **third**, entirely separate
fingerprint function, deliberately not reusing `Signal`'s or
`DetectionResult`'s own fingerprint logic (§27 of the task explicitly:
"Do NOT reuse Signal fingerprinting for Resolution. Resolution has its own
identity.") — proven directly
(`test_fingerprint_is_a_distinct_function_from_signal_and_detection_fingerprints`).
Same `detection_id` + concluded `remediation_strategy` + `subject` →
same fingerprint, so re-running Resolution over an unchanged
`DetectionResult` (the same incident, diagnosed the same way) doesn't
create an uncontrolled duplicate plan
(`test_repeated_run_over_same_detection_does_not_duplicate`) — `_finalize()`
checks `ResolutionGateway.find_by_fingerprint()` before saving, same
pattern `DetectingAgent`/`MonitoringAgent` established.

## 16. Security

- **No shell/subprocess anywhere** in `app/agents/incident_resolution.py`
  (verified structurally).
- **No mutation capabilities**: `WRITE_CODE`, `DEPLOY`, `CREATE_COMMIT`,
  `WRITE_JIRA`, `RESOLVE_INCIDENT`, `ROLLBACK`, `RUN_TESTS` are all absent
  from `IncidentResolutionAgent.capabilities` — verified directly.
- **Bounded evidence**: `detection.supporting_signal_ids` is truncated to
  `settings.incident_resolution_max_evidence` (default 50) before any
  resolution is attempted — defense in depth even though Detecting already
  bounds the set it produces
  (`test_evidence_bounded_by_configured_ceiling`, seeded with 80 signals).
- **No raw metadata forwarded**: reuses `signal_to_evidence_dict` from
  `app.agents.detecting` — `Signal.metadata` (the free-form bucket) never
  reaches the prompt (`test_evidence_dict_does_not_include_raw_signal_metadata`).
- **No arbitrary query surface**: `ResolutionInput` has exactly one field,
  `detection_id` — no filter/query string anywhere
  (`test_resolution_input_has_no_raw_query_surface`).
- **High-risk auto-remediation rejected**: §10 above.

## 17. Capability model

`IncidentResolutionAgent.capabilities = {READ_DETECTION, READ_SIGNALS,
READ_ARTIFACT, QUERY_KNOWLEDGE, WRITE_RESOLUTION}`. Two new capabilities
were added, both justified inline in `app/agent_runtime/capabilities.py`:
`READ_DETECTION` (reading a persisted `DetectionResult` — the read-only
counterpart to `WRITE_DETECTION`, same split as `READ_ARTIFACT`/
`WRITE_ARTIFACT`) and `WRITE_RESOLUTION` (permission to persist this
agent's own output, mirroring `WRITE_DETECTION`'s role — not a real-world
mutation capability). **Important distinction**: this agent deliberately
does **not** hold the pre-existing `RESOLVE_INCIDENT` capability — that
capability is reserved for whatever future component actually closes out
/executes remediation for an incident. `IncidentResolutionAgent` only ever
proposes a plan, so granting it `RESOLVE_INCIDENT` would misrepresent what
it's authorized to do.

## 18. Standalone execution

Requires only `AgentInput` + `AgentContext` (with `detections`/`signals`/
`artifacts`/`resolutions`/`knowledge` populated) — no
`OrchestrationService` dependency anywhere in `_perform()`. Registered in
`app/orchestration/registry_setup.py` (resolvable via
`registry.get("incident_resolution_agent")`) but **not** added to
`STAGE_ORDER`/`STAGE_TO_AGENT_ID` — it is not an SDLC stage, and the
happy-path `SequentialAgent`/`LoopAgent` were not touched.

## 19. Future orchestration / human escalation

Not implemented here. The eventual flow:

```
ResolutionResult
      │
      ▼
OrchestrationService
      │
 ┌────┼────────────┐
 ▼    ▼             ▼
Codegen Testing  Deployment
```

`OrchestrationService` remains the only component that routes agents — no
code here invokes `CodegenAgent`/`TestingAgent`/`DeploymentAgent` directly,
and no human-escalation UI/notification mechanism exists. `escalation_recommended=True`
and `target_agent=None` are the entire signal a future human-review
surface would consume.

## 20. Google services

| Google Service | Quipu Role | Used by IncidentResolutionAgent? |
|---|---|---|
| Google ADK | Agent runtime | Yes — `_incident_resolution_llm_agent` |
| Gemini | Diagnosis/remediation reasoning | Yes — the genuine reasoning task this agent exists for |
| Agent Search | Enterprise knowledge | Yes, on-demand via the existing `query_enterprise_knowledge` tool and `incident_resolution_agent` retrieval profile (both already existed) |
| Firestore | Durable state | Yes — `FirestoreResolutionRepository`, reused pattern |
| Cloud Monitoring / Cloud Logging / Cloud Run | Production telemetry / deployment | Indirect only — this agent consumes evidence Monitoring/Deployment already normalized into `Signal`/`Artifact`; it never calls any of these APIs directly |

## 21. Testing

`tests/test_resolution_domain.py` (28), `tests/test_resolution_persistence.py`
(18), `tests/test_incident_resolution_agent.py` (40) — 86 new tests
covering: domain validation (including the deliberately-missing
`CONFIGURATION_CHANGE` value), fingerprint determinism/distinctness from
both other fingerprint functions, repository contract, input validation
(missing detection, `FEATURE_OPPORTUNITY` rejected before Gemini, missing
signal), evidence resolution (partial/total signal loss, deployment-
artifact correlation, original `DetectionResult` never mutated), reasoning
scenarios (deployment regression → rollback, application defect →
code_fix, architecture defect → architecture_review, test defect → retest,
insufficient evidence → escalate), **the full deterministic safety policy**
(arbitrary target agent rejected, invalid strategy rejected at schema
level, fabricated signal/artifact ids dropped, high confidence cannot
bypass missing evidence, rollback without target escalates, code_fix
without code evidence escalates, high risk always escalates, low
confidence with high severity still escalates), knowledge-vs-evidence
separation, LLM failure/empty/malformed/invalid-confidence handling,
deduplication, and security (bounded evidence, no raw metadata leakage, no
query-string injection surface). No test requires live Gemini/Google
credentials.

Full suite: **693 passed, 6 skipped** (pre-existing gated integration
tests) — no regressions.
