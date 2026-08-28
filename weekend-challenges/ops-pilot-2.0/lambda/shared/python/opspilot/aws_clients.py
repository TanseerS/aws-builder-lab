"""Lazily-created, module-level boto3 clients.

Clients are expensive to build and safe to reuse across Lambda invocations, so
they are created once per execution environment and shared. This is the single
place in the codebase where boto3 clients are constructed.
"""

from __future__ import annotations

from functools import cache
from typing import Any

import boto3
from botocore.config import Config

from . import config

_BOTO_CONFIG = Config(
    retries={"max_attempts": 3, "mode": "standard"},
    connect_timeout=5,
    read_timeout=30,
    user_agent_extra="opspilot/2.0",
)

# Bedrock inference can legitimately take longer than a control-plane call.
_BEDROCK_CONFIG = Config(
    retries={"max_attempts": 1, "mode": "standard"},  # retries handled explicitly
    connect_timeout=5,
    read_timeout=60,
    user_agent_extra="opspilot/2.0",
)


@cache
def client(service: str) -> Any:
    """Return a cached boto3 client for ``service``."""
    cfg = _BEDROCK_CONFIG if service == "bedrock-runtime" else _BOTO_CONFIG
    return boto3.client(service, region_name=config.AWS_REGION, config=cfg)


@cache
def resource(service: str) -> Any:
    """Return a cached boto3 resource for ``service``."""
    return boto3.resource(service, region_name=config.AWS_REGION, config=_BOTO_CONFIG)


def table(name: str) -> Any:
    """Return a DynamoDB Table resource by name."""
    return resource("dynamodb").Table(name)
