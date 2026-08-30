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
LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")


def main() -> int:
    try:
        import google.genai as genai

        client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
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
