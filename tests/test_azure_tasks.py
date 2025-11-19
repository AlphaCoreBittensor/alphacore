import pytest
from alphacore.benchmark.providers.azure.azure_blobcontainer_create import azure_blobcontainer_create
from alphacore.benchmark.providers.azure.azure_functionapp_create import azure_functionapp_create
from alphacore.benchmark.providers.azure.azure_networksecuritygroup_create import azure_networksecuritygroup_create
from alphacore.benchmark.providers.azure.azure_queue_create import azure_queue_create
from alphacore.benchmark.providers.azure.azure_resourcegroup_create import azure_resourcegroup_create
from alphacore.benchmark.providers.azure.azure_roleassignment_create import azure_roleassignment_create
from alphacore.benchmark.providers.azure.azure_storageaccount_create import azure_storageaccount_create
from alphacore.benchmark.providers.azure.azure_subnet_create import azure_subnet_create
from alphacore.benchmark.providers.azure.azure_userassignedidentity_create import azure_userassignedidentity_create
from alphacore.benchmark.providers.azure.azure_virtualnetwork_create import azure_virtualnetwork_create


def test_azure_blobcontainer_create():
    spec = azure_blobcontainer_create("testid", {})
    assert spec.kind == "azure.blobcontainer.create"
    assert spec.verify_fn is not None


def test_azure_functionapp_create():
    spec = azure_functionapp_create("testid", {})
    assert spec.kind == "azure.functionapp.create"
    assert spec.verify_fn is not None


def test_azure_networksecuritygroup_create():
    spec = azure_networksecuritygroup_create("testid", {})
    assert spec.kind == "azure.networksecuritygroup.create"
    assert spec.verify_fn is not None


def test_azure_queue_create():
    spec = azure_queue_create("testid", {})
    assert spec.kind == "azure.queue.create"
    assert spec.verify_fn is not None


def test_azure_resourcegroup_create():
    spec = azure_resourcegroup_create("testid", {})
    assert spec.kind == "azure.resourcegroup.create"
    assert spec.verify_fn is not None


def test_azure_roleassignment_create():
    spec = azure_roleassignment_create("testid", {})
    assert spec.kind == "azure.roleassignment.create"
    assert spec.verify_fn is not None


def test_azure_storageaccount_create():
    spec = azure_storageaccount_create("testid", {})
    assert spec.kind == "azure.storageaccount.create"
    assert spec.verify_fn is not None


def test_azure_subnet_create():
    spec = azure_subnet_create("testid", {})
    assert spec.kind == "azure.subnet.create"
    assert spec.verify_fn is not None


def test_azure_userassignedidentity_create():
    spec = azure_userassignedidentity_create("testid", {})
    assert spec.kind == "azure.userassignedidentity.create"
    assert spec.verify_fn is not None


def test_azure_virtualnetwork_create():
    spec = azure_virtualnetwork_create("testid", {})
    assert spec.kind == "azure.virtualnetwork.create"
    assert spec.verify_fn is not None
