import random
import re

from modules.generation.terraform.providers.gcp import helpers
from modules.generation.terraform.providers.gcp.resources import (
    secret_manager_secret,
    secret_manager_secret_iam,
)
from modules.generation.terraform.resource_templates import TemplateContext


class TestSecretManagerSecret:
    def test_secret_template_exists(self):
        templates = secret_manager_secret.get_templates()
        assert len(templates) == 1
        assert templates[0].key == "secret_manager_secret"
        assert templates[0].kind == "secret manager secret"

    def test_secret_builds_with_version(self):
        ctx = TemplateContext(
            nonce="test12345678",
            task_id="task001",
            rng=random.Random(42),
            shared={},
            validator_sa="validator@test.com",
        )
        instance = secret_manager_secret._build_secret(ctx)

        assert len(instance.invariants) == 2
        assert instance.invariants[0].resource_type == "google_secret_manager_secret"
        assert instance.invariants[1].resource_type == "google_secret_manager_secret_version"
        assert "values.secret_id" in instance.invariants[0].match
        assert "values.secret_data" in instance.invariants[1].match

    def test_secret_id_helper(self):
        secret_id = helpers.secret_id("abc123")
        assert re.fullmatch(r"[a-z][a-z0-9]{7,23}", secret_id)
        assert secret_id == helpers.secret_id("abc123")

    def test_secret_payload_helper(self):
        payload = helpers.secret_payload("testnonce12345678")
        assert re.fullmatch(r"[a-z0-9]{16,24}", payload)


class TestSecretManagerSecretIAM:
    def test_secret_iam_template_exists(self):
        templates = secret_manager_secret_iam.get_templates()
        assert len(templates) == 1
        assert templates[0].key == "secret_manager_secret_iam"
        assert templates[0].requires == ("secret_manager_secret",)

    def test_secret_iam_builds_with_dependencies(self):
        ctx = TemplateContext(
            nonce="test12345678",
            task_id="task001",
            rng=random.Random(42),
            shared={
                "secret_manager_secret": {"secret_id": "my-secret"},
                "service_account": {"account_id": "my-sa"},
            },
            validator_sa="validator@test.com",
        )
        instance = secret_manager_secret_iam._bind_secret_accessor(ctx)

        assert len(instance.invariants) == 1
        inv = instance.invariants[0]
        assert inv.resource_type == "google_secret_manager_secret_iam_member"
        assert inv.match["values.secret_id"] == "my-secret"
        assert "validator@test.com" in inv.match["values.member"]

    def test_secret_iam_role_helper(self):
        role = helpers.secret_iam_role(random.Random(42))
        assert role.startswith("roles/secretmanager")
