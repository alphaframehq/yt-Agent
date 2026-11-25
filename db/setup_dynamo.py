# LOCAL PRODUCTION PATCH — 2026-11-25
# Source: alphaframehq/yt-Agent main @ c39f42541b8e784dcf41c09583dc57227be07667 — db/setup_dynamo.py
# Fixes: P0 audit #25 (task #27): hardcoded region_name='us-east-1' removed; region is the single env var AWS_REGION (from .env.example) with no hardcoded fallback — missing AWS_REGION raises; boto3 missing / client failure / table-create failure / TTL failure / any table missing afterwards all raise instead of returning skipped/failed dicts.
# Applied via PR #1 (branch fix/production-mocks), 2026-11-25.
# No secrets in this file — env placeholders / env-var names only.

"""DynamoDB 7-Table Provisioned Setup Script for TubePilot AI.

Creates all tables in PROVISIONED billing mode (5 RCU / 5 WCU per table)
to prevent surprise bills from pay-per-request spikes.
Configures GSI indexes and TTL lifecycle expiration rules.

Tables:
1. yt_videos (PK: video_id, GSI: status-created-index)
2. yt_research_cache (PK: topic_hash, TTL: ttl [7 days])
3. yt_verification_log (PK: video_id, SK: iteration, TTL: ttl [60 days])
4. yt_pipeline_runs (PK: run_id, TTL: ttl [90 days])
5. yt_config (PK: config_id)
6. yt_bot_requests (PK: request_id, TTL: ttl [7 days])
7. yt_topics_queue (PK: topic_id)

Region: single env var ``AWS_REGION`` (see .env.example) — no hardcoded
region and no fallback. Missing AWS_REGION, missing boto3, a failed
table create, or a table missing afterwards all raise; this script
never returns a "skipped"/"failed" dict.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List
from loguru import logger

from tools.secrets import get_secret

PROVISIONED_THROUGHPUT = {
    "ReadCapacityUnits": 5,
    "WriteCapacityUnits": 5,
}

_TABLE_NAMES: List[str] = [
    "yt_videos",
    "yt_research_cache",
    "yt_verification_log",
    "yt_pipeline_runs",
    "yt_config",
    "yt_bot_requests",
    "yt_topics_queue",
]


def _aws_region() -> str:
    """Return the single AWS region source of truth (env var AWS_REGION).

    Raises:
        RuntimeError: If AWS_REGION is not set — there is no hardcoded
            fallback region, so a missing value must fail loudly
            instead of provisioning tables in the wrong region.
    """
    region = os.environ.get("AWS_REGION")
    if not region:
        raise RuntimeError(
            "AWS_REGION env var is required (single source of truth for "
            "the DynamoDB region); set it in .env, e.g. AWS_REGION=ap-southeast-2"
        )
    return region


def _verify_tables_exist(client, table_names: List[str]) -> None:
    """Raise if any expected table is missing after provisioning."""
    for table_name in table_names:
        try:
            client.describe_table(TableName=table_name)
        except Exception as exc:
            raise RuntimeError(
                f"DynamoDB table '{table_name}' is missing after setup: {exc}"
            ) from exc


def setup_all_tables() -> Dict[str, str]:
    """Idempotently create all 7 DynamoDB tables with provisioned capacity and TTL."""
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError as exc:
        raise RuntimeError("boto3 is required to provision DynamoDB tables") from exc

    region = _aws_region()
    aws_key = get_secret("AWS_ACCESS_KEY_ID")
    aws_sec = get_secret("AWS_SECRET_ACCESS_KEY")
    session_kwargs: Dict[str, Any] = {"region_name": region}
    if aws_key and aws_sec:
        session_kwargs["aws_access_key_id"] = aws_key
        session_kwargs["aws_secret_access_key"] = aws_sec

    try:
        dynamo = boto3.client("dynamodb", **session_kwargs)
    except Exception as e:
        raise RuntimeError(f"Could not connect to AWS DynamoDB in {region}: {e}") from e

    results = {}

    # 1. yt_videos (with status-created-index GSI)
    _create_table(
        dynamo,
        table_name="yt_videos",
        key_schema=[{"AttributeName": "video_id", "KeyType": "HASH"}],
        attribute_definitions=[
            {"AttributeName": "video_id", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "N"},
        ],
        gsi=[{
            "IndexName": "status-created-index",
            "KeySchema": [
                {"AttributeName": "status", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
            "ProvisionedThroughput": PROVISIONED_THROUGHPUT,
        }],
    )

    # 2. yt_research_cache (TTL: ttl)
    _create_table(
        dynamo,
        table_name="yt_research_cache",
        key_schema=[{"AttributeName": "topic_hash", "KeyType": "HASH"}],
        attribute_definitions=[{"AttributeName": "topic_hash", "AttributeType": "S"}],
        ttl_attribute="ttl",
    )

    # 3. yt_verification_log (PK: video_id, SK: iteration, TTL: ttl)
    _create_table(
        dynamo,
        table_name="yt_verification_log",
        key_schema=[
            {"AttributeName": "video_id", "KeyType": "HASH"},
            {"AttributeName": "iteration", "KeyType": "RANGE"},
        ],
        attribute_definitions=[
            {"AttributeName": "video_id", "AttributeType": "S"},
            {"AttributeName": "iteration", "AttributeType": "N"},
        ],
        ttl_attribute="ttl",
    )

    # 4. yt_pipeline_runs (TTL: ttl)
    _create_table(
        dynamo,
        table_name="yt_pipeline_runs",
        key_schema=[{"AttributeName": "run_id", "KeyType": "HASH"}],
        attribute_definitions=[{"AttributeName": "run_id", "AttributeType": "S"}],
        ttl_attribute="ttl",
    )

    # 5. yt_config
    _create_table(
        dynamo,
        table_name="yt_config",
        key_schema=[{"AttributeName": "config_id", "KeyType": "HASH"}],
        attribute_definitions=[{"AttributeName": "config_id", "AttributeType": "S"}],
    )

    # 6. yt_bot_requests (TTL: ttl)
    _create_table(
        dynamo,
        table_name="yt_bot_requests",
        key_schema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
        attribute_definitions=[{"AttributeName": "request_id", "AttributeType": "S"}],
        ttl_attribute="ttl",
    )

    # 7. yt_topics_queue
    _create_table(
        dynamo,
        table_name="yt_topics_queue",
        key_schema=[{"AttributeName": "topic_id", "KeyType": "HASH"}],
        attribute_definitions=[{"AttributeName": "topic_id", "AttributeType": "S"}],
    )

    # Verify every table actually exists — a silent "success" after a
    # failed create would leave the runtime looking in the wrong place.
    _verify_tables_exist(dynamo, _TABLE_NAMES)

    logger.info("All 7 DynamoDB table configurations verified.")
    return {"status": "success", "tables": "7 tables configured with provisioned 5 RCU/WCU"}


def _create_table(
    client,
    table_name: str,
    key_schema: List[Dict[str, str]],
    attribute_definitions: List[Dict[str, str]],
    gsi: Optional[List[Dict[str, Any]]] = None,
    ttl_attribute: Optional[str] = None,
):
    """Helper to idempotently create a table and configure TTL."""
    try:
        kwargs: Dict[str, Any] = {
            "TableName": table_name,
            "KeySchema": key_schema,
            "AttributeDefinitions": attribute_definitions,
            "BillingMode": "PROVISIONED",
            "ProvisionedThroughput": PROVISIONED_THROUGHPUT,
        }
        if gsi:
            kwargs["GlobalSecondaryIndexes"] = gsi

        client.create_table(**kwargs)
        logger.info(f"DynamoDB table '{table_name}' creation initiated.")
    except client.exceptions.ResourceInUseException:
        logger.info(f"DynamoDB table '{table_name}' already exists.")
    except Exception as e:
        raise RuntimeError(f"Failed to create DynamoDB table '{table_name}': {e}") from e

    # Configure TTL if requested
    if ttl_attribute:
        try:
            client.update_time_to_live(
                TableName=table_name,
                TimeToLiveSpecification={
                    "Enabled": True,
                    "AttributeName": ttl_attribute,
                },
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to configure TTL for DynamoDB table '{table_name}': {exc}") from exc


if __name__ == "__main__":
    setup_all_tables()
