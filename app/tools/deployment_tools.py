"""Controlled Cloud Run deployment tool — the ONLY way any Quipu agent
deploys anything. No shell, no `gcloud` subprocess, no arbitrary command
surface exists: the model supplies a small set of individually-validated,
typed arguments; application code builds the actual Cloud Run image URI and
Service definition and calls the real Cloud Run Admin API directly.

Enforces, in order:
1. DEPLOY capability granted
2. service_name matches Cloud Run's naming rules
3. region is in the configured allow-list
4. environment is in the configured allow-list
5. image_tag matches a safe Docker-tag charset — the full image URI is built
   by this tool from CLOUD_RUN_IMAGE_REGISTRY + service_name + tag; the
   model never supplies an image URI directly, so it cannot point a
   deployment at an arbitrary/untrusted image.
6. cpu/memory match Cloud Run's supported value sets
7. min/max instances are within configured bounds

Returns a result dict rather than raising on rejection, so the model sees
the failure and can react — a rejected request never calls Cloud Run.
Every real (or attempted) deployment is also appended to
tool_context.state["_deployment_results"] — this is the ground-truth record
DeploymentAgent uses to build the final artifact, never the model's own
narration of what it thinks happened.
"""

import re

from google.adk.tools import ToolContext

from app.agent_runtime.capabilities import AgentCapability
from app.config import get_settings
from app.core.cloud_run_client import CloudRunConfigError, CloudRunDeployer, CloudRunDeploymentError

_SERVICE_NAME_RE = re.compile(r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")
_IMAGE_TAG_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$")
_ALLOWED_CPU = {"1", "2", "4", "8"}
_ALLOWED_MEMORY_RE = re.compile(r"^(512Mi|1Gi|2Gi|4Gi|8Gi)$")


def _rejected(field: str, message: str) -> dict:
    return {"success": False, "status": "error", "error": f"{field}: {message}"}


async def deploy_cloud_run(
    service_name: str,
    region: str,
    environment: str,
    image_tag: str,
    cpu: str,
    memory: str,
    min_instances: int,
    max_instances: int,
    tool_context: ToolContext,
) -> dict:
    """Deploy a new Cloud Run revision. image_tag is a short version/tag
    string only (e.g. "v1", "abc123") — this tool builds the full image URI
    from the configured registry; you cannot supply an arbitrary image URI
    and there is no shell command surface at all. cpu must be one of
    1/2/4/8; memory one of 512Mi/1Gi/2Gi/4Gi/8Gi. Deployment only proceeds
    if every argument passes Quipu's configured deployment policy.
    """
    granted: set[AgentCapability] = tool_context.state.get("_capabilities", set())
    if AgentCapability.DEPLOY not in granted:
        return _rejected("capability", "DEPLOY capability not granted")

    settings = get_settings()

    if not _SERVICE_NAME_RE.match(service_name):
        return _rejected("service_name", f"'{service_name}' is not a valid Cloud Run service name")
    if region not in settings.cloud_run_allowed_regions:
        return _rejected("region", f"'{region}' is not in the allowed region list {settings.cloud_run_allowed_regions}")
    if environment not in settings.cloud_run_allowed_environments:
        return _rejected(
            "environment", f"'{environment}' is not an allowed deployment environment {settings.cloud_run_allowed_environments}"
        )
    if not _IMAGE_TAG_RE.match(image_tag):
        return _rejected("image_tag", f"'{image_tag}' is not a valid image tag")
    if cpu not in _ALLOWED_CPU:
        return _rejected("cpu", f"'{cpu}' is not an allowed CPU value {sorted(_ALLOWED_CPU)}")
    if not _ALLOWED_MEMORY_RE.match(memory):
        return _rejected("memory", f"'{memory}' is not an allowed memory value")
    if not (0 <= min_instances <= max_instances <= settings.cloud_run_max_instances_ceiling):
        return _rejected(
            "instances", f"min_instances/max_instances must satisfy 0 <= min <= max <= {settings.cloud_run_max_instances_ceiling}"
        )
    if not settings.cloud_run_image_registry:
        return _rejected("configuration", "CLOUD_RUN_IMAGE_REGISTRY is not configured")

    image = f"{settings.cloud_run_image_registry}/{service_name}:{image_tag}"

    deployer = tool_context.state.get("_cloud_run_deployer")
    if deployer is None:
        try:
            deployer = CloudRunDeployer()
        except CloudRunConfigError as exc:
            result = _rejected("configuration", str(exc))
            tool_context.state.setdefault("_deployment_results", []).append(result)
            return result

    try:
        deploy_result = await deployer.deploy(
            service_name=service_name,
            region=region,
            image=image,
            cpu=cpu,
            memory=memory,
            min_instances=min_instances,
            max_instances=max_instances,
        )
    except CloudRunDeploymentError as exc:
        result = {
            "success": False,
            "status": "error",
            "error": str(exc),
            "service_name": service_name,
            "region": region,
            "image": image,
        }
        tool_context.state.setdefault("_deployment_results", []).append(result)
        return result

    result = {
        "success": True,
        "status": deploy_result.status,  # "succeeded" | "failed" — ground truth from Cloud Run itself
        "service_name": deploy_result.service_name,
        "project": deploy_result.project,
        "region": deploy_result.region,
        "revision": deploy_result.revision,
        "uri": deploy_result.uri,
        "message": deploy_result.message,
        "deployed_at": deploy_result.deployed_at.isoformat(),
        "image": image,
    }
    tool_context.state.setdefault("_deployment_results", []).append(result)
    return result


DEPLOYMENT_TOOLS = [deploy_cloud_run]
