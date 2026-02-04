import random
import re

import pytest

from modules.generation.terraform.providers.gcp import helpers
from modules.generation.terraform.providers.gcp.resources import (
    logging_project_sink,
)
from modules.generation.terraform.resource_templates import TemplateContext


class TestLoggingProjectSink:
    def test_logging_sink_template_exists(self):
        templates = logging_project_sink.get_templates()
        assert len(templates) == 1
        assert templates[0].key == "logging_project_sink"
        assert templates[0].kind == "logging project sink"

    def test_logging_sink_with_bucket_destination(self):
        ctx = TemplateContext(
            nonce="test12345678",
            task_id="task001",
            rng=random.Random(42),
            shared={"bucket": {"name": "my-bucket"}},
        )
        instance = logging_project_sink._build_logging_sink(ctx)

        assert len(instance.invariants) == 1
        inv = instance.invariants[0]
        assert inv.resource_type == "google_logging_project_sink"
        assert "my-bucket" in inv.match["values.destination"]
        assert "values.filter" in inv.match

    def test_logging_sink_without_bucket_fails(self):
        ctx = TemplateContext(
            nonce="test12345678",
            task_id="task001",
            rng=random.Random(42),
            shared={},
        )
        with pytest.raises(RuntimeError, match="requires an existing bucket"):
            logging_project_sink._build_logging_sink(ctx)

    def test_logging_filter_helper(self):
        filter_val = helpers.logging_filter(random.Random(123))
        assert filter_val in helpers.LOGGING_FILTERS

    def test_logging_sink_name_helper(self):
        name = helpers.logging_sink_name("jkl012")
        assert re.fullmatch(r"[a-z][a-z0-9]{7,23}", name)
        assert name == helpers.logging_sink_name("jkl012")
