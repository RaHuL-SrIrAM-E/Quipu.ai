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

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
