import random
import re

from modules.generation.terraform.providers.gcp import helpers
from modules.generation.terraform.providers.gcp.resources import (
    dns_record_set,
)
from modules.generation.terraform.resource_templates import TemplateContext


class TestDNSRecordSet:
    def test_dns_record_template_exists(self):
        templates = dns_record_set.get_templates()
        assert len(templates) == 1
        assert templates[0].key == "dns_record_set"
        assert templates[0].requires == ("dns_zone",)

    def test_dns_record_builds_with_zone(self):
        ctx = TemplateContext(
            nonce="test12345678",
            task_id="task001",
            rng=random.Random(42),
            shared={"dns_zone": {"name": "my-zone"}},
        )
        instance = dns_record_set._build_dns_record(ctx)

        assert len(instance.invariants) == 1
        inv = instance.invariants[0]
        assert inv.resource_type == "google_dns_record_set"
        assert inv.match["values.managed_zone"] == "my-zone"
        assert "acore.example." in inv.match["values.name"]
        assert inv.match["values.type"] in helpers.DNS_RECORD_TYPES

    def test_dns_zone_name_helper(self):
        name = helpers.dns_zone_name("mno345")
        assert re.fullmatch(r"[a-z][a-z0-9]{5,13}", name)
        assert name == helpers.dns_zone_name("mno345")

    def test_dns_record_ttl_helper(self):
        ttl = helpers.dns_record_ttl(random.Random(42))
        assert ttl in helpers.DNS_RECORD_TTLS
        assert isinstance(ttl, int)
        assert ttl > 0
