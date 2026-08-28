"""Thin Cloud Run Admin API v2 client wrapper — the ONLY place in the
repository allowed to import google.cloud.run_v2. No `gcloud` subprocess,
no shell — this calls the real Cloud Run Admin API directly, the same
pattern already used for Jira (app/core/jira_client.py), Agent Search
(app/knowledge/backends/google_search.py), and Firestore
(app/persistence/firestore/client.py): a small dataclass result, structured
errors translated at the boundary, Application Default Credentials only.

deploy_cloud_run (app/tools/deployment_tools.py) is the only caller — it
constructs the full image URI and validates every argument *before* this
class is ever invoked; this class does not re-validate anything, it only
executes exactly the update-service operation it's given.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from google.api_core import exceptions as google_exceptions
from google.cloud import run_v2

from app.config import get_settings


class CloudRunConfigError(Exception):
    pass


class CloudRunDeploymentError(Exception):
    pass


@dataclass
class CloudRunDeployResult:
    status: str  # "succeeded" | "failed" — ground truth from Cloud Run's own terminal_condition
    service_name: str
    project: str
    region: str
    revision: str | None
    uri: str | None
    message: str
    deployed_at: datetime


class CloudRunDeployer:
    def __init__(self, client: run_v2.ServicesAsyncClient | None = None):
        settings = get_settings()
        if not settings.gcp_project_id:
            raise CloudRunConfigError("GCP_PROJECT_ID is not set")
        self.project_id = settings.gcp_project_id
        self._settings = settings
        self._client = client

    def _get_client(self) -> run_v2.ServicesAsyncClient:
        if self._client is None:
            self._client = run_v2.ServicesAsyncClient()
        return self._client

    async def deploy(
        self,
        *,
        service_name: str,
        region: str,
        image: str,
        cpu: str,
        memory: str,
        min_instances: int,
        max_instances: int,
        env_vars: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> CloudRunDeployResult:
        client = self._get_client()
        service_path = f"projects/{self.project_id}/locations/{region}/services/{service_name}"

        container = run_v2.Container(
            image=image,
            resources=run_v2.ResourceRequirements(limits={"cpu": cpu, "memory": memory}),
            env=[run_v2.EnvVar(name=key, value=value) for key, value in (env_vars or {}).items()],
        )
        template = run_v2.RevisionTemplate(
            containers=[container],
            scaling=run_v2.RevisionScaling(min_instance_count=min_instances, max_instance_count=max_instances),
        )
        service = run_v2.Service(name=service_path, template=template, labels=labels or {})

        try:
            operation = await client.update_service(service=service, timeout=self._settings.cloud_run_deploy_timeout_seconds)
            result_service = await operation.result(timeout=self._settings.cloud_run_deploy_timeout_seconds)
        except google_exceptions.GoogleAPICallError as exc:
            raise CloudRunDeploymentError(str(exc)) from exc
        except TimeoutError as exc:
            raise CloudRunDeploymentError(f"deployment timed out: {exc}") from exc

        condition = result_service.terminal_condition
        succeeded = condition.state == run_v2.Condition.State.CONDITION_SUCCEEDED

        return CloudRunDeployResult(
            status="succeeded" if succeeded else "failed",
            service_name=service_name,
            project=self.project_id,
            region=region,
            revision=result_service.latest_ready_revision or None,
            uri=result_service.uri or None,
            message=condition.message or "",
            deployed_at=datetime.now(timezone.utc),
        )
