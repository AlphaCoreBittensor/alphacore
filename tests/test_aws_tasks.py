import pytest
from alphacore.benchmark.providers.aws.aws_cloudwatchloggroup_create import aws_cloudwatchloggroup_create
from alphacore.benchmark.providers.aws.aws_eventbridge_rule_create import aws_eventbridge_rule_create
from alphacore.benchmark.providers.aws.aws_iaminstanceprofile_create import aws_iaminstanceprofile_create
from alphacore.benchmark.providers.aws.aws_iamrole_create import aws_iamrole_create
from alphacore.benchmark.providers.aws.aws_s3bucket_create import aws_s3bucket_create
from alphacore.benchmark.providers.aws.aws_securitygroup_create import aws_securitygroup_create
from alphacore.benchmark.providers.aws.aws_snstopic_create import aws_snstopic_create
from alphacore.benchmark.providers.aws.aws_sqsqueue_create import aws_sqsqueue_create
from alphacore.benchmark.providers.aws.aws_ssmparameter_create import aws_ssmparameter_create
from alphacore.benchmark.providers.aws.aws_vpc_create import aws_vpc_create

def test_aws_cloudwatchloggroup_create():
    spec = aws_cloudwatchloggroup_create("testid", {})
    assert spec.kind == "aws.cloudwatchloggroup.create"
    assert spec.verify_fn is not None

def test_aws_eventbridge_rule_create():
    spec = aws_eventbridge_rule_create("testid", {})
    assert spec.kind == "aws.eventbridge_rule.create"
    assert spec.verify_fn is not None

def test_aws_iaminstanceprofile_create():
    spec = aws_iaminstanceprofile_create("testid", {})
    assert spec.kind == "aws.iaminstanceprofile.create"
    assert spec.verify_fn is not None

def test_aws_iamrole_create():
    spec = aws_iamrole_create("testid", {})
    assert spec.kind == "aws.iamrole.create"
    assert spec.verify_fn is not None

def test_aws_s3bucket_create():
    spec = aws_s3bucket_create("testid", {})
    assert spec.kind == "aws.s3bucket.create"
    assert spec.verify_fn is not None

def test_aws_securitygroup_create():
    spec = aws_securitygroup_create("testid", {})
    assert spec.kind == "aws.securitygroup.create"
    assert spec.verify_fn is not None

def test_aws_snstopic_create():
    spec = aws_snstopic_create("testid", {})
    assert spec.kind == "aws.snstopic.create"
    assert spec.verify_fn is not None

def test_aws_sqsqueue_create():
    spec = aws_sqsqueue_create("testid", {})
    assert spec.kind == "aws.sqsqueue.create"
    assert spec.verify_fn is not None

def test_aws_ssmparameter_create():
    spec = aws_ssmparameter_create("testid", {})
    assert spec.kind == "aws.ssmparameter.create"
    assert spec.verify_fn is not None

def test_aws_vpc_create():
    spec = aws_vpc_create("testid", {})
    assert spec.kind == "aws.vpc.create"
    assert spec.verify_fn is not None
