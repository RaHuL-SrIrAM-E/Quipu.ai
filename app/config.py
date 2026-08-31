import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Loaded before Settings (and before any app.agents.* module, which all
# import this module first) — so .env can override GOOGLE_GENAI_USE_VERTEXAI
# below, and load_dotenv() never overwrites a real environment variable
# that's already set (e.g. one set by Cloud Run's own env-var config).
load_dotenv()

# google-genai — which every ADK LlmAgent uses internally (see
# app/agents/*.py, app/orchestration/adk/decision_agent.py) — reads this
# environment variable DIRECTLY, not through the Settings class below, to
# decide Vertex AI+ADC vs. the Gemini Developer API+API key. Setting it
# here (not merely documenting it) is what actually makes every LlmAgent
# construction site use ADC-only Vertex AI by default, in every
# environment, unless a deployer explicitly opts out. See
# docs/deployment/gcp.md §4. setdefault() never overrides an operator's
# own explicit choice (e.g. deliberately testing against the Gemini
# Developer API with GOOGLE_GENAI_USE_VERTEXAI=false) — and has no effect
# on the test suite either way, since every LlmAgent's real client
# construction is monkeypatched out at the InMemoryRunner seam in tests,
# never actually reaching google-genai's credential resolution.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Quipu"
    environment: str = "development"

    database_url: str = "sqlite:///./quipu.db"
    workspace_root: str = "./.quipu_workspaces"

    # The single target repository Planning/Architecture/Codegen/Testing check
    # out into a per-workflow workspace (app.orchestration.service.
    # OrchestrationService._ensure_workspace, app.core.repo.clone_repo). A
    # Ticket may override either via its own metadata (repo_url/repo_ref) —
    # see _ensure_workspace — but there is no multi-repo domain model here;
    # this is deliberately the smallest config for a single-target-repo
    # deployment, not a repository registry. default_repo_ref is optional
    # (None clones the repo's default branch).
    default_repo_url: str | None = None
    default_repo_ref: str | None = None

    # A fine-scoped Git hosting access token (e.g. a GitHub fine-grained PAT
    # or GitHub App installation token), sourced from Secret Manager via a
    # Cloud Run env var — same operational pattern as JIRA_API_TOKEN below.
    # Never embedded in a clone URL or written to .git/config — see
    # app.core.repo.clone_repo's use of it via short-lived, process-scoped
    # git config (GIT_CONFIG_* env vars passed only to the git subprocess),
    # never persisted to disk. None is valid for a public repo.
    git_access_token: str | None = None

    # Lets a debugging session inspect a failed workflow's checked-out
    # workspace after the fact instead of it being reclaimed the moment the
    # workflow reaches a terminal status. Never disable this in normal
    # operation — ephemeral Cloud Run disk is not a place to accumulate
    # workspaces indefinitely.
    workspace_cleanup_enabled: bool = True

    gcp_project_id: str | None = None
    gcp_location: str = "us-central1"
    # The one centralized model id every LlmAgent construction site reads
    # (app/agents/*.py, app/orchestration/adk/decision_agent.py) — never
    # hardcoded per-agent, accessed via Vertex AI (see the
    # GOOGLE_GENAI_USE_VERTEXAI env var set below, not a Settings field)
    # rather than a developer API key. Deterministic agents
    # (MonitoringAgent) have no LlmAgent and therefore never read this
    # setting. "gemini-3.5-flash" (the prior default) 404s against live
    # Vertex AI in quipu-507109/us-central1 — live-verified against that
    # project on 2026-08-30; "gemini-2.5-flash" and "gemini-2.5-pro" both
    # work. Re-verify before bumping to a newer model.
    gemini_model: str = "gemini-2.5-flash"
    google_application_credentials: str | None = None

    jira_base_url: str | None = None
    jira_email: str | None = None
    jira_api_token: str | None = None
    jira_project_key: str | None = None

    # Agent Search (formerly Vertex AI Search / Discovery Engine) — used only by
    # app/knowledge/backends/google_search.py, the Google retrieval adapter.
    discovery_engine_data_store_id: str | None = None
    discovery_engine_location: str = "global"
    discovery_engine_serving_config_id: str = "default_search"
    discovery_engine_timeout_seconds: float = 10.0

    # Firestore — used only by app/persistence/firestore/*.py, for Quipu's own
    # operational/workflow state (NOT enterprise knowledge; see
    # docs/architecture/persistence.md). None uses Firestore's default database.
    # For local dev against the Firestore emulator, set the standard
    # FIRESTORE_EMULATOR_HOST env var (read automatically by the Google client).
    firestore_database_id: str | None = None

    # app/tools/testing_tools.py — bounds a single controlled test-runner
    # invocation so a hung test suite can't hang the whole agent.
    test_execution_timeout_seconds: float = 120.0

    # app/orchestration/ — bounded retry/recovery limits. Named per stage
    # rather than one global number, since a flaky test suite and a genuinely
    # broken design fail for different reasons and may warrant different budgets.
    max_codegen_retries: int = 2
    max_test_retries: int = 2
    max_architecture_replans: int = 1
    max_deployment_retries: int = 2
    orchestration_loop_max_iterations: int = 3

    # Cloud Run — used only by app/core/cloud_run_client.py, the Google
    # deployment adapter. cloud_run_image_registry is app-controlled: the
    # deployment tool builds the full image URI from this prefix + a
    # model-supplied tag, so the model never supplies an arbitrary image URI.
    cloud_run_image_registry: str | None = None
    cloud_run_allowed_regions: list[str] = ["us-central1"]
    cloud_run_allowed_environments: list[str] = ["development", "staging", "production"]
    cloud_run_max_instances_ceiling: int = 10
    cloud_run_deploy_timeout_seconds: float = 300.0

    # Cloud Monitoring / Cloud Logging — used only by
    # app/core/cloud_monitoring_client.py and app/core/cloud_logging_client.py,
    # MonitoringAgent's Google observability adapters. Reuses
    # cloud_run_allowed_regions/cloud_run_allowed_environments above as the
    # scope boundary (Monitoring only ever observes Cloud Run services, so a
    # second allow-list would just duplicate that one) — the model can never
    # widen these through MonitoringInput. Windows/result counts are bounded
    # so a query can't turn into unrestricted production data extraction.
    monitoring_default_window_minutes: int = 15
    monitoring_max_window_minutes: int = 1440  # 24h ceiling
    monitoring_log_query_limit: int = 50
    monitoring_log_query_max_limit: int = 200
    monitoring_min_log_severity: str = "ERROR"
    monitoring_error_rate_warning_threshold: float = 0.05  # 5% — operational collection policy, not AI reasoning; see docs/architecture/monitoring_agent.md §10
    monitoring_error_rate_critical_threshold: float = 0.15
    monitoring_api_timeout_seconds: float = 30.0

    # Detecting Agent — used only by app/agents/detecting.py. Deliberately a
    # separate, larger window ceiling than monitoring_max_window_minutes
    # above: Monitoring bounds a *live Cloud API* query (hours at most is
    # sensible); Detecting reads already-persisted Signals and product
    # detection genuinely needs weeks-scale windows (see
    # docs/architecture/detecting_agent.md §4). detecting_max_signals bounds
    # how much evidence is ever assembled into one Gemini prompt — the
    # context-management ceiling, never a raw Signal dump.
    detecting_default_window_minutes: int = 60
    detecting_max_window_minutes: int = 43200  # 30 days
    detecting_max_signals: int = 50

    # Incident Resolution Agent — used only by app/agents/incident_resolution.py.
    # incident_resolution_max_evidence bounds how many of a DetectionResult's
    # supporting_signal_ids are ever resolved/sent to Gemini (defense in
    # depth — Detecting already bounds this via detecting_max_signals, but
    # Resolution doesn't trust that ceiling blindly). The confidence
    # threshold is the deterministic auto-remediation gate documented in
    # docs/architecture/incident_resolution_agent.md §10 — below it, any
    # non-escalation strategy is downgraded to ESCALATE regardless of what
    # Gemini proposed.
    incident_resolution_max_evidence: int = 50
    incident_resolution_min_confidence_for_auto_remediation: float = 0.7

    # Pub/Sub — used only by app/eventing/google_pubsub_client.py, the
    # Google event-transport adapter for Signal ingestion (see
    # docs/architecture/pubsub_signal_ingestion.md). Auth via Application
    # Default Credentials, same as every other Google integration above.
    # Reuses gcp_project_id — Pub/Sub lives in the same GCP project as
    # everything else. pubsub_max_message_bytes bounds untrusted message
    # size before it's even JSON-decoded; pubsub_dead_letter_topic is
    # optional deployment-time policy (configured on the subscription
    # itself), never required for local tests.
    pubsub_signal_topic: str | None = None
    pubsub_signal_subscription: str | None = None
    pubsub_dead_letter_topic: str | None = None
    pubsub_max_message_bytes: int = 262144  # 256 KiB
    pubsub_pull_max_messages: int = 20
    pubsub_api_timeout_seconds: float = 30.0

    # Pub/Sub Signal Consumer Worker — used only by
    # app/eventing/worker.py, the long-running process boundary around
    # SignalIngestionService (see docs/architecture/pubsub_worker.md).
    # pubsub_worker_max_concurrency bounds how many messages are
    # in-flight (parsed/normalized/persisted) at once, independent of
    # pubsub_pull_max_messages (how many are fetched in one pull() call —
    # a pull can return more than max_concurrency; the extra simply wait
    # for a concurrency slot). pubsub_worker_poll_interval_seconds is how
    # long the worker sleeps after an empty pull before trying again
    # (interruptible by stop()). pubsub_worker_shutdown_timeout_seconds
    # bounds how long stop() waits for in-flight messages to finish
    # before cancelling them.
    pubsub_worker_max_concurrency: int = 10
    pubsub_worker_poll_interval_seconds: float = 5.0
    pubsub_worker_shutdown_timeout_seconds: float = 30.0

    # Control Plane API — used only by app/api/ (see
    # docs/architecture/control_plane_api.md). api_cors_allow_origins is
    # deliberately empty by default (no CORS grant at all) rather than "*"
    # — a deployer must explicitly list the UI's real origin(s).
    # api_default_page_size/api_max_page_size bound every collection
    # endpoint so a caller can never request an unbounded Firestore scan.
    # api_auth_mode is the honest, minimal boundary this level ships:
    # "development" trusts a caller-supplied identity HEADER for
    # audit/attribution only (never a privilege flag — see
    # app/api/auth.py) and grants the fixed capability set a human
    # reviewer needs; a real deployment sets api_auth_mode="disabled" to
    # refuse every authenticated endpoint until a real identity provider
    # is wired in behind the same dependency.
    api_cors_allow_origins: list[str] = []
    api_default_page_size: int = 50
    api_max_page_size: int = 200
    api_auth_mode: str = "development"
    # Explicit opt-in only (never auto-detected from ui/dist existing on
    # disk — that made `pytest` behavior depend on whether someone had
    # locally run `npm run build`, which is exactly the kind of
    # environment-dependent flakiness this flag avoids). Set true only in
    # the production Docker image, which always bundles ui/dist. See
    # docs/architecture/control_plane_ui.md "Cloud Run deployment".
    api_serve_ui: bool = False

    # Detection Processor — used only by app/detection/ (the event-driven
    # DetectionTrigger -> DetectingAgent boundary). Reuses
    # detecting_max_signals/detecting_max_window_minutes (DetectingAgent's
    # own ceilings, app/agents/detecting.py) rather than duplicating them;
    # these are the NEW per-domain knobs the processor itself owns: the
    # default evidence window per DetectionDomain (product signals
    # meaningfully accumulate over days/weeks; operational anomalies need a
    # short recent window), and the minimum number of related signals
    # required before DetectingAgent (and therefore Gemini) is invoked at
    # all — see docs/architecture/event_driven_detection.md "Aggregation".
    detection_operational_window_minutes: int = 30
    detection_product_window_minutes: int = 10080  # 7 days
    detection_min_operational_signals: int = 1
    detection_min_product_signals: int = 2

    # Remediation Verification — used only by app/verification/. Governs
    # how long after a remediation deployment to look for production
    # evidence, and the safety floor below which "we don't have enough
    # data" is the only allowed conclusion (never VERIFIED_RESOLVED).
    # verification_latency_p99_threshold_ms is the "future policy addition"
    # MonitoringAgent's own latency signal deliberately deferred (see
    # app/agents/monitoring.py._collect_metrics) — implemented narrowly
    # here, for verification's comparison only, not inside MonitoringAgent.
    verification_window_minutes: int = 30
    verification_minimum_post_deployment_signals: int = 1
    verification_max_signals_per_condition: int = 20
    verification_latency_p99_threshold_ms: float = 500.0

    # Resilience layer — used only by app/core/resilience/ and the exact
    # external-boundary call sites it wraps (see
    # docs/architecture/resilience.md). Google Cloud SDK clients already
    # have their own timeout= kwargs (unrelated to this section);
    # llm_call_timeout_seconds is the one genuine gap this closes — the
    # ADK runner loop (every agent's Gemini call) had no bound before.
    llm_call_timeout_seconds: float = 60.0

    # CodegenAgent's own budget — deliberately separate from
    # llm_call_timeout_seconds above. Its ADK run_async() loop covers a
    # multi-turn conversation (explore the checked-out repo via REPO_TOOLS,
    # then one or more write_file calls via CODEGEN_TOOLS) rather than the
    # single-shot exploration Planning/Architecture do, so the shared 60s
    # budget that comfortably fits those two is measurably tight for
    # Codegen (see docs/architecture — 2026-08-31 production timeout
    # investigation: Planning+Architecture together ran ~73s, Codegen hit
    # the 60s ceiling). Planning/Architecture/DetectingAgent/
    # IncidentResolutionAgent/the orchestration decision agent all remain
    # on llm_call_timeout_seconds, unaffected by this setting.
    codegen_llm_call_timeout_seconds: float = 120.0

    # TestingAgent's own budget — same rationale as codegen_llm_call_
    # timeout_seconds above. Its ADK conversation wraps a run_tests tool
    # call that already has its own test_execution_timeout_seconds (120.0,
    # unchanged) — nesting that inside the shared 60s budget made the
    # inner 120s bound unreachable in practice, since the outer wrapper
    # would fire first on any real test run taking more than ~60s. 150.0
    # = test_execution_timeout_seconds + 25% headroom for the LLM's own
    # turns (invoking run_tests, interpreting the result).
    testing_llm_call_timeout_seconds: float = 150.0

    # DeploymentAgent's own budget — same rationale again. Its ADK
    # conversation wraps a deploy_cloud_run tool call bounded by
    # cloud_run_deploy_timeout_seconds (300.0, unchanged) — the same
    # nesting problem, at larger scale. 360.0 = cloud_run_deploy_timeout_
    # seconds + 20% headroom for the LLM's own turns.
    deployment_llm_call_timeout_seconds: float = 360.0

    # Opt-in, per-agent demo/mock execution — 2026-08-31 hackathon
    # determinism hardening. Independent flags (deliberately not one
    # global DEMO_MODE) so Codegen and Testing can be toggled separately.
    # False is the only production default; when False, CodegenAgent/
    # TestingAgent run their existing real LLM/tool-calling path exactly
    # as before this setting existed — this flag changes nothing about
    # that path. When True, the agent skips its real Gemini conversation
    # entirely (no ADK runner, no with_timeout wait) and deterministically
    # produces a CODE_CHANGE/TEST_RESULT artifact of the same shape a real
    # run would, still consuming the real upstream artifact (Architecture/
    # CodegenOutput) and the real provisioned workspace — see
    # app.agents.codegen._demo_codegen_response / app.agents.testing.
    # _demo_testing_response. Never changes PlanningAgent, ArchitectureAgent,
    # DeploymentAgent, or any other agent.
    codegen_demo_mode: bool = False
    testing_demo_mode: bool = False

    # Same convention, added separately (2026-08-31) once Deployment became
    # reachable in the E2E demo: real DeploymentAgent requires
    # CLOUD_RUN_IMAGE_REGISTRY and, if configured, would call the real Cloud
    # Run Admin API (app/core/cloud_run_client.py) against a target-app image
    # that no pipeline in this codebase builds/pushes — not something to
    # exercise live in a hackathon demo. False is the only production
    # default; when False, DeploymentAgent's real deploy_cloud_run/
    # CloudRunDeployer path is unchanged. When True, DeploymentAgent skips
    # its real Gemini conversation AND the deploy_cloud_run tool/
    # CloudRunDeployer entirely (structural bypass, not a prompt
    # instruction) and deterministically produces a DEPLOYMENT artifact of
    # the same shape a real run would, consuming the real upstream
    # CodegenOutput — see app.agents.deployment._demo_deployment_response.
    # Never changes PlanningAgent, ArchitectureAgent, CodegenAgent, or
    # TestingAgent.
    deployment_demo_mode: bool = False

    jira_retry_max_attempts: int = 3
    jira_retry_base_delay_seconds: float = 0.5
    jira_circuit_breaker_failure_threshold: int = 5
    jira_circuit_breaker_recovery_timeout_seconds: float = 30.0

    # Demo scenario seeding — used only by app/api/routes/demo.py.
    # Explicitly opt-in (default False, same convention as
    # api_serve_ui): the route is not even registered on the FastAPI app
    # unless this is true, so it 404s rather than merely being
    # "disabled" when off. Never enable in a real production deployment.
    demo_endpoints_enabled: bool = False

    # Control Plane API — run-to-completion command
    # (POST /workflows/{id}/run, app/api/routes/workflows.py). Bounds how
    # many execute_next_step() iterations one request may perform, so a
    # misbehaving workflow can never turn one HTTP request into an
    # unbounded loop.
    workflow_run_max_iterations: int = 20

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
