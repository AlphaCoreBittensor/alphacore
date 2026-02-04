import random
import re

from modules.generation.terraform.providers.gcp import helpers
from modules.generation.terraform.providers.gcp.resources import (
    custom_iam_role,
)
from modules.generation.terraform.resource_templates import TemplateContext


class TestCustomIAMRole:
    def test_custom_role_template_exists(self):
        templates = custom_iam_role.get_templates()
        assert len(templates) == 1
        assert templates[0].key == "custom_iam_role"
        assert templates[0].kind == "custom iam role"

    def test_custom_role_builds(self):
        ctx = TemplateContext(
            nonce="test12345678",
            task_id="task001",
            rng=random.Random(42),
            shared={},
        )
        instance = custom_iam_role._build_custom_role(ctx)

        assert len(instance.invariants) == 1
        inv = instance.invariants[0]
        assert inv.resource_type == "google_project_iam_custom_role"
        assert "values.role_id" in inv.match
        assert "values.permissions.0" in inv.match
        assert isinstance(inv.match["values.permissions.0"], str)

    def test_custom_role_id_helper(self):
        role_id = helpers.custom_role_id("ghi789")
        assert re.fullmatch(r"[a-z][a-z0-9]{7,23}", role_id)
        assert role_id == helpers.custom_role_id("ghi789")

    def test_custom_role_permissions_helper(self):
        perms = helpers.custom_role_permissions(random.Random(42))
        assert isinstance(perms, list)
        assert len(perms) > 0
        assert all("." in p for p in perms)
