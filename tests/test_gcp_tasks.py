import pytest
from alphacore.benchmark.providers.gcp.gcp_storagebucket_create import gcp_storagebucket_create
from alphacore.benchmark.providers.gcp.gcp_serviceaccount_create import gcp_serviceaccount_create
from alphacore.benchmark.providers.gcp.gcp_iambinding_create import gcp_iambinding_create
from alphacore.benchmark.providers.gcp.gcp_pubsubtopic_create import gcp_pubsubtopic_create
from alphacore.benchmark.providers.gcp.gcp_pubsubsubscription_create import gcp_pubsubsubscription_create
from alphacore.benchmark.providers.gcp.gcp_vpcnetwork_create import gcp_vpcnetwork_create
from alphacore.benchmark.providers.gcp.gcp_subnetwork_create import gcp_subnetwork_create
from alphacore.benchmark.providers.gcp.gcp_firewallrule_create import gcp_firewallrule_create
from alphacore.benchmark.providers.gcp.gcp_cloudrunservice_create import gcp_cloudrunservice_create
from alphacore.benchmark.providers.gcp.gcp_artifactregistryrepo_create import gcp_artifactregistryrepo_create


def test_gcp_storagebucket_create():
    spec = gcp_storagebucket_create("testid", {})
    assert spec.kind == "gcp.storagebucket.create"
    assert spec.verify_fn is not None


def test_gcp_serviceaccount_create():
    spec = gcp_serviceaccount_create("testid", {})
    assert spec.kind == "gcp.serviceaccount.create"
    assert spec.verify_fn is not None


def test_gcp_iambinding_create():
    spec = gcp_iambinding_create("testid", {})
    assert spec.kind == "gcp.iambinding.create"
    assert spec.verify_fn is not None


def test_gcp_pubsubtopic_create():
    spec = gcp_pubsubtopic_create("testid", {})
    assert spec.kind == "gcp.pubsubtopic.create"
    assert spec.verify_fn is not None


def test_gcp_pubsubsubscription_create():
    spec = gcp_pubsubsubscription_create("testid", {})
    assert spec.kind == "gcp.pubsubsubscription.create"
    assert spec.verify_fn is not None


def test_gcp_vpcnetwork_create():
    spec = gcp_vpcnetwork_create("testid", {})
    assert spec.kind == "gcp.vpcnetwork.create"
    assert spec.verify_fn is not None


def test_gcp_subnetwork_create():
    spec = gcp_subnetwork_create("testid", {})
    assert spec.kind == "gcp.subnetwork.create"
    assert spec.verify_fn is not None


def test_gcp_firewallrule_create():
    spec = gcp_firewallrule_create("testid", {})
    assert spec.kind == "gcp.firewallrule.create"
    assert spec.verify_fn is not None


def test_gcp_cloudrunservice_create():
    spec = gcp_cloudrunservice_create("testid", {})
    assert spec.kind == "gcp.cloudrunservice.create"
    assert spec.verify_fn is not None


def test_gcp_artifactregistryrepo_create():
    spec = gcp_artifactregistryrepo_create("testid", {})
    assert spec.kind == "gcp.artifactregistryrepo.create"
    assert spec.verify_fn is not None
