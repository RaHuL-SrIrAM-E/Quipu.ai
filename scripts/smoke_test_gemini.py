#!/usr/bin/env python3
"""One-shot Vertex AI / Gemini smoke test for deployment validation.

Standalone script, not part of the production import graph (nothing under
app/ imports scripts/, and this file is not collected by pytest). Run it
manually after deployment to confirm Vertex AI connectivity and auth:

    python scripts/smoke_test_gemini.py

Uses Application Default Credentials only — no API keys or service-account
JSON files. Requires GOOGLE_GENAI_USE_VERTEXAI=true and a project/location
reachable via ADC (e.g. `gcloud auth application-default login` locally, or
the deployment's service identity in GCP).
"""

import os
import sys

PROJECT = os.environ.get("GCP_PROJECT_ID", "quipu-507109")
# Deliberately NOT defaulted to a specific region (e.g. "us-central1"): no
# real agent construction site (app/agents/*.py, via ADK's own
# Gemini.api_client) ever passes an explicit project/location to
# google.genai.Client either — every one relies on the SDK's own default
# resolution, which is location="global" when unset. Forcing this script to
# a specific region would validate a call shape the real agents don't
# actually make (see app/config.py's gemini_model comment) — e.g.
# "gemini-3.5-flash" 404s under an explicit "us-central1" but works under
# the SDK's own "global" default, live-verified 2026-08-31. Only set
# GCP_LOCATION explicitly if you want to validate a specific forced region.
LOCATION = os.environ.get("GCP_LOCATION")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")


def main() -> int:
    try:
        import google.genai as genai

        client_kwargs = {"vertexai": True, "project": PROJECT}
        if LOCATION:
            client_kwargs["location"] = LOCATION
        client = genai.Client(**client_kwargs)
        response = client.models.generate_content(
            model=MODEL,
            contents="Reply with exactly one short sentence confirming you are working.",
        )
        print("SUCCESS")
        print(response.text)
        return 0
    except Exception as exc:
        print("FAILURE")
        print(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
