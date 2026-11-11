"""Agent spend: usage-type classification.

Matching is on usage-type shapes, never on model names, and the shared services
only count when the row carries an agent tag.
"""

from __future__ import annotations

import pytest

from clont.finops.aws.ai_usage_types import agent_bucket

_BEDROCK = "Amazon Bedrock"


@pytest.mark.parametrize(
    ("service", "usage_type", "bucket"),
    [
        (_BEDROCK, "USE1-Claude3.5Sonnet-input-tokens", "tokens-in"),
        (_BEDROCK, "USE1-Claude3.5Sonnet-output-tokens", "tokens-out"),
        (_BEDROCK, "USE1-Claude3.5Sonnet-cache-read-input-token-count", "cache-read"),
        (_BEDROCK, "USE1-Claude3.5Sonnet-cache-write-input-token-count", "cache-write"),
        # marketplace spelling of a third-party model
        ("Claude 3.5 Sonnet (Amazon Bedrock Edition)", "USE1-MP:USE1_InputTokenCount-Units", "tokens-in"),
        ("Claude 3.5 Sonnet (Amazon Bedrock Edition)", "USE1-MP:USE1_CacheReadInputTokenCount-Units", "cache-read"),
        # a model nobody has heard of still buckets
        (_BEDROCK, "EUW1-SomeNewModel9-input-tokens", "tokens-in"),
        (_BEDROCK, "USE1-ProvisionedThroughput-ModelUnits-Hours", "floor"),
        ("Amazon OpenSearch Service", "USE1-OCU-Hours", "floor"),
        ("Amazon OpenSearch Service", "USE1-SearchOCU", "floor"),
        ("Amazon OpenSearch Service", "USE1-IndexingOCU", "index"),
        ("Amazon OpenSearch Service", "USE1-TimedStorage-ByteHrs", "index"),
        ("Amazon OpenSearch Service", "USE1-ESInstance:r6g.large", "floor"),
        ("Amazon SageMaker", "USE1-Host:ml.m5.large", "floor"),
        ("Amazon SageMaker", "USE1-ServerlessInf:Mem-4GB", "plumbing"),
        ("Amazon Kendra", "USE1-Kendra-EnterpriseEdition-Hours", "floor"),
        ("Amazon Kendra", "USE1-Kendra-Connector-DocumentsScanned", "plumbing"),
    ],
)
def test_ai_services_bucket_without_a_tag(service, usage_type, bucket):
    assert agent_bucket(service, usage_type) == bucket


def test_sagemaker_endpoint_hours_are_a_floor_not_plumbing():
    assert agent_bucket("Amazon SageMaker", "USE1-Host:ml.g5.xlarge") == "floor"
    assert agent_bucket("Amazon SageMaker", "USE1-Host:VolumeUsage.gp2") == "floor"


def test_matching_ignores_case():
    assert agent_bucket("amazon bedrock", "use1-claude3-haiku-OUTPUT-TOKENS") == "tokens-out"


@pytest.mark.parametrize(
    ("service", "usage_type", "bucket"),
    [
        ("AWS Lambda", "USE1-Lambda-GB-Second", "plumbing"),
        ("AWS Lambda", "USE1-Request", "plumbing"),
        ("AWS Lambda", "USE1-Lambda-Provisioned-Concurrency", "floor"),
        ("AWS Step Functions", "USE1-StateTransition", "plumbing"),
        ("AmazonCloudWatch", "USE1-DataProcessing-Bytes", "plumbing"),
        ("Amazon Elastic Compute Cloud", "USE1-NatGateway-Hours", "floor"),
        ("Amazon Virtual Private Cloud", "USE1-VpcEndpoint-Hours", "floor"),
    ],
)
def test_shared_services_count_only_when_tagged(service, usage_type, bucket):
    assert agent_bucket(service, usage_type, tagged=True) == bucket
    assert agent_bucket(service, usage_type) == ""


@pytest.mark.parametrize(
    ("service", "usage_type"),
    [
        ("Amazon Elastic Compute Cloud", "BoxUsage:t3.micro"),
        ("Amazon Simple Storage Service", "TimedStorage-ByteHrs"),
        ("Amazon Relational Database Service", "InstanceUsage:db.t3.micro"),
        (_BEDROCK, ""),
    ],
)
def test_not_agent_spend(service, usage_type):
    assert agent_bucket(service, usage_type) == ""
    # a tag alone does not make an ec2 box agent spend
    assert agent_bucket(service, usage_type, tagged=True) == ""
