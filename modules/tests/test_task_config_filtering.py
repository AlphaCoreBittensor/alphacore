import os
from textwrap import dedent

import pytest

from modules.generation.yaml_config import reset_yaml_config
from modules.generation.terraform.providers.gcp import task_bank
from modules.generation.terraform.providers.gcp import composite_resource_bank, single_resource_bank


@pytest.fixture(autouse=True)
def reset_global_caches(monkeypatch):
    """
    Ensure template/capability caches and YAML config are reset between tests.
    """
    reset_yaml_config()
    monkeypatch.setattr(task_bank, "_TEMPLATE_CACHE", None, raising=False)
    monkeypatch.setattr(task_bank, "_CAPABILITY_CACHE", None, raising=False)
    yield
    reset_yaml_config()
    monkeypatch.setattr(task_bank, "_TEMPLATE_CACHE", None, raising=False)
    monkeypatch.setattr(task_bank, "_CAPABILITY_CACHE", None, raising=False)


def test_composite_bank_filters_service_account_templates(tmp_path, monkeypatch):
    cfg = tmp_path / "task_config.yaml"
    cfg.write_text(
        dedent(
            """
            providers:
              gcp:
                enabled: true
                task_banks:
                  single_resource:
                    enabled: false
                  composite_resource:
                    enabled: true
                    min_resources: 2
                    max_resources: 3
                    families:
                      network_stack:
                        enabled: true
                      service_account_delivery:
                        enabled: false
                      bucket_object_with_iam:
                        enabled: false
                      project_with_iam:
                        enabled: false
                      network_plus_service_account:
                        enabled: false
            settings:
              repository:
                path: ./tasks
            """
        ).strip()
    )
    monkeypatch.setenv("ALPHACORE_CONFIG", str(cfg))

    bank = composite_resource_bank._build_bank()

    family_names = {f.name for f in bank.families}
    assert "network_stack" in family_names
    assert "service_account_delivery" not in family_names
    assert "bucket_object_with_iam" not in family_names
    assert "project_with_iam" not in family_names
    assert "network_plus_service_account" not in family_names

    templates = set(bank.templates.keys())
    assert "service_account" not in templates
    assert "bucket_iam_member" not in templates
    assert "project_iam_member" not in templates


def test_single_bank_filters_to_enabled_resources(tmp_path, monkeypatch):
    cfg = tmp_path / "task_config.yaml"
    cfg.write_text(
        dedent(
            """
            providers:
              gcp:
                enabled: true
                task_banks:
                  single_resource:
                    enabled: true
                    resources:
                      - storage_bucket
                      - artifact_repository
                      - dns_managed_zone
                      - secret_manager_secret
                  composite_resource:
                    enabled: false
            settings:
              repository:
                path: ./tasks
            """
        ).strip()
    )
    monkeypatch.setenv("ALPHACORE_CONFIG", str(cfg))

    bank = single_resource_bank._build_bank()

    family_names = {f.name for f in bank.families}
    assert family_names == {"single_bucket", "single_artifact_repo", "single_dns_zone", "single_secret"}

    templates = set(bank.templates.keys())
    assert templates <= {"storage_bucket", "artifact_repository", "dns_managed_zone", "secret_manager_secret"}
    assert "compute_instance_basic" not in templates
