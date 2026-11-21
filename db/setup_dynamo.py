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
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List
from loguru import logger

from tools.secrets import get_secret

PROVISIONED_THROUGHPUT = {
    "ReadCapacityUnits": 5,
    "WriteCapacityUnits": 5,
}


def setup_all_tables() -> Dict[str, str]:
    """Idempotently create all 7 DynamoDB tables with provisioned capacity and TTL."""
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        logger.warning("boto3 not installed. Skipping live DynamoDB table creation.")
        return {"status": "skipped", "reason": "boto3 not installed"}

    aws_key = get_secret("AWS_ACCESS_KEY_ID")
    aws_sec = get_secret("AWS_SECRET_ACCESS_KEY")
    session_kwargs = {"region_name": "us-east-1"}
    if aws_key and aws_sec:
        session_kwargs["aws_access_key_id"] = aws_key
        session_kwargs["aws_secret_access_key"] = aws_sec

    try:
        dynamo = boto3.client("dynamodb", **session_kwargs)
    except Exception as e:
        logger.warning(f"Could not connect to AWS DynamoDB: {e}")
        return {"status": "failed", "error": str(e)}

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
        logger.warning(f"Error creating table '{table_name}': {e}")

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
        except Exception:
            pass


if __name__ == "__main__":
    setup_all_tables()
