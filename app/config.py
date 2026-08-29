from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Quipu"
    environment: str = "development"

    database_url: str = "sqlite:///./quipu.db"
    workspace_root: str = "./.quipu_workspaces"

    gcp_project_id: str | None = None
    gcp_location: str = "us-central1"
    gemini_model: str = "gemini-2.5-pro"
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

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
