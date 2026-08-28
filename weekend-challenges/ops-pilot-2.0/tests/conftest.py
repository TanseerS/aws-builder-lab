"""Test configuration: put the shared layer on the import path.

The shared library ships as a Lambda layer laid out as ``python/opspilot``,
which is exactly how it appears at ``/opt/python`` in the runtime. Tests import
it from the same place, so what is tested is what is deployed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAYER = ROOT / "lambda" / "shared" / "python"

sys.path.insert(0, str(LAYER))

# Configuration is read at import time, so it must be set before opspilot loads.
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("PROJECT_NAME", "opspilot")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("RESOURCE_PREFIX", "opspilot-test")
os.environ.setdefault("INCIDENTS_TABLE", "opspilot-test-incidents")
os.environ.setdefault("CHANGES_TABLE", "opspilot-test-changes")
os.environ.setdefault("ARTIFACTS_BUCKET", "opspilot-test-artifacts")
os.environ.setdefault("EVENT_BUS_NAME", "opspilot-test-events")
os.environ.setdefault("DEMO_FUNCTION_NAME", "opspilot-test-demo-app")
os.environ.setdefault("DEMO_TABLE_NAME", "opspilot-test-demo-table")
os.environ.setdefault("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
os.environ.setdefault("MAX_PROMPT_CHARS", "18000")
