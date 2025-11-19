"""
Natural language instruction generation for Terraform tasks.

The generator prefers using the OpenAI ChatGPT API when an API key is
configured; otherwise it falls back to a deterministic summary that still
covers the invariants and submission requirements.
"""

from __future__ import annotations

import json
import logging
import os
import re
import textwrap
from typing import Optional

from alphacore.benchmark.core.task.models import Invariant, TerraformTask

try:  # pragma: no cover - optional dependency
    from openai import OpenAI
except ImportError:  # pragma: no cover - optional dependency
    OpenAI = None  # type: ignore[assignment]

LOGGER = logging.getLogger(__name__)
DEFAULT_MODEL = os.getenv("ALPHACORE_TASK_PROMPT_MODEL", "gpt-4o-mini")
DISALLOWED_TERMS = (
    "readme",
    "prerequisite",
    "documentation",
    "tutorial",
    "guide",
    "walkthrough",
)
PROVIDER_SYNONYMS = {
    "aws": ("aws", "amazon web services"),
    "azure": ("azure", "microsoft azure"),
    "gcp": ("gcp", "google cloud", "google cloud platform"),
}


class TaskInstructionGenerator:
    """
    Generates user-facing instructions for a Terraform task.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: float = 0.6,
        enable_llm: bool = True,
    ) -> None:
        self.temperature = temperature
        self.model = model or DEFAULT_MODEL
        self.enable_llm = enable_llm
        self.api_key = os.getenv("ALPHACORE_OPENAI_API_KEY") or os.getenv(
            "OPENAI_API_KEY"
        )
        self._client = None
        if self.enable_llm and OpenAI is not None and self.api_key:
            try:
                self._client = OpenAI(api_key=self.api_key)
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.warning("Failed to initialise OpenAI client: %s", exc)
                self._client = None

    def generate(self, task: TerraformTask, task_name: Optional[str] = None) -> str:
        """
        Create miner-facing instructions describing the task.
        """
        context = self._build_context(task, task_name)
        fallback = self._to_plain_text(self._fallback_instructions(task))
        if self.enable_llm and not self._client:
            LOGGER.warning(
                "LLM instruction generator is enabled but no OpenAI client is available; using fallback instructions."
            )
        if not self._client:
            return fallback

        system_prompt = (
            "You design short infrastructure assignments for expert Terraform engineers. "
            "Describe the goal in natural language, mention resource requirements explicitly, "
            "reiterate how the submission bundle must look, and respond using only plain ASCII text. "
            "Do not emit markdown, bullet lists, numbered lists, emojis, or decorative characters. "
            "Avoid using the word 'invariant', do not repeat the same requirement twice, "
            "and never introduce deliverables beyond the provided resource specs and submission instructions (for example, do not mention README files)."
        )
        user_prompt = textwrap.dedent(
            f"""
            Compose miner instructions for the following task metadata.

            {context}

            Requirements:
            - Mention every resource requirement explicitly, matching the invariant field values listed and referring to the resource using the natural language kind label.
            - State how the miner must package the work: single archive, Terraform code at the repository root, and terraform.tfstate inside the archive.
            - Keep it under 220 words and prefer imperative voice.
            - Produce plain sentences with no markdown or special formatting characters.
            - Do not add requirements or artefacts beyond what is described above.
            """
        ).strip()

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=400,
                n=1,
            )
            message = response.choices[0].message.content if response.choices else ""
            cleaned = self._to_plain_text(message or "")
            try:
                return self._enforce_allowed_content(cleaned, task)
            except ValueError as exc:
                LOGGER.warning("LLM instructions rejected: %s", exc)
                if self.enable_llm:
                    raise RuntimeError("LLM instructions failed validation.") from exc
                return fallback
        except Exception as exc:  # pragma: no cover - network failure fallback
            LOGGER.warning("LLM instruction request failed: %s", exc)
            if self.enable_llm:
                raise RuntimeError("LLM instruction request failed.") from exc
            return fallback

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _build_context(self, task: TerraformTask, task_name: Optional[str]) -> str:
        spec = task.spec
        requirements = self._format_invariants(spec.invariants)
        kind_description = self._describe_kind(spec.kind)
        submission_details = self._format_submission_details(task)
        return textwrap.dedent(
            f"""
            Provider: {task.provider}
            Resource kind: {kind_description}
            Resource requirements:
            {requirements or 'None'}
            Submission details:
            {submission_details}
            """
        ).strip()

    def _fallback_instructions(self, task: TerraformTask) -> str:
        submission_details = self._format_submission_details(task)
        requirement_lines = self._format_invariants(task.spec.invariants)
        provider = task.provider.upper()
        kind_description = self._describe_kind(task.spec.kind)
        submission_block = textwrap.dedent(
            f"""
            Submission details: {submission_details}
            Return only the Terraform configuration and the terraform.tfstate file.
            """
        ).strip()
        return "\n\n".join(
            [
                f"Use Terraform on {provider} to build a {kind_description} that satisfies the constraints below.",
                "Resource requirements:",
                requirement_lines,
                submission_block,
            ]
        ).strip()

    @staticmethod
    def _format_invariants(invariants: list[Invariant]) -> str:
        lines = []
        for invariant in invariants:
            fields = ", ".join(
                f"{TaskInstructionGenerator._humanize_field(k)} equals {json.dumps(v, ensure_ascii=True)}"
                for k, v in invariant.match.items()
            )
            lines.append(fields)
        return "\n".join(lines) if lines else "No requirement details provided."

    @staticmethod
    def _format_submission_details(task: TerraformTask) -> str:
        submit = task.to_dict().get("submit_requirements", {})
        layout = submit.get("bundle_layout", {"state": "terraform.tfstate"})
        package_format = submit.get("package_format", "archive")
        state_path = layout.get("state", "terraform.tfstate")
        extra_layout = {k: v for k, v in layout.items() if k != "state"}
        layout_desc = (
            ", ".join(f"{key} is {value}" for key, value in extra_layout.items())
            if extra_layout
            else ""
        )
        parts = [
            f"Return a single {package_format} with the Terraform project rooted at the repository root so terraform plan runs from that location.",
            f"Place terraform.tfstate at {state_path} inside the archive.",
        ]
        if layout_desc:
            parts.append(f"Preserve additional layout requirements: {layout_desc}.")
        parts.append("Exclude extra documents or artefacts.")
        return " ".join(parts)

    @staticmethod
    def _humanize_field(field: str) -> str:
        stripped = field.split(".")[-1]
        return stripped.replace("_", " ")

    @staticmethod
    def _describe_kind(kind: str) -> str:
        if not kind:
            return "resource"
        parts = kind.split(".")
        provider = parts[0] if parts else ""
        remainder = parts[1:]
        if remainder and remainder[-1].lower().startswith("v"):
            remainder.pop()
        resource = remainder or ["resource"]
        provider_label = {
            "aws": "AWS",
            "gcp": "Google Cloud Platform",
            "azure": "Microsoft Azure",
        }.get(provider.lower(), provider.upper())
        resource_label = " ".join(part.replace("_", " ") for part in resource)
        description = f"{provider_label} {resource_label}".strip()
        return description.strip()

    @staticmethod
    def _to_plain_text(text: str) -> str:
        """
        Strip Markdown-like formatting and limit output to ASCII sentences.
        """
        cleaned = text.replace("**", "").replace("__", "").replace("`", "")
        cleaned = cleaned.encode("ascii", "ignore").decode("ascii")
        cleaned = re.sub(r"(?i)invariants?", "requirements", cleaned)
        lines = []
        for raw_line in cleaned.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            line = re.sub(r"^(\d+\.\s+|[-*]\s+)", "", line)
            line = line.replace("#", "")
            lines.append(line)
        return "\n".join(lines).strip()

    def _enforce_allowed_content(self, text: str, task: TerraformTask) -> str:
        if not text:
            raise ValueError("LLM produced empty instructions.")
        lowered = text.lower()
        for term in DISALLOWED_TERMS:
            if term in lowered:
                raise ValueError(f"LLM output contained disallowed term: {term}")
        provider_terms = self._provider_terms(task)
        if provider_terms and not any(term in lowered for term in provider_terms):
            raise ValueError(
                f"LLM output missing required detail: {task.provider.lower()}"
            )
        for required in self._required_terms(task):
            if required not in lowered:
                raise ValueError(f"LLM output missing required detail: {required}")
        return text

    @staticmethod
    def _required_terms(task: TerraformTask) -> set[str]:
        required = {
            "archive",
            "terraform.tfstate",
        }
        for invariant in task.spec.invariants:
            for value in invariant.match.values():
                required.add(str(value).lower())
        return required

    @staticmethod
    def _provider_terms(task: TerraformTask) -> set[str]:
        provider_slug = task.provider.lower()
        synonyms = PROVIDER_SYNONYMS.get(provider_slug)
        if not synonyms:
            return {provider_slug}
        return {synonym.lower() for synonym in synonyms}
