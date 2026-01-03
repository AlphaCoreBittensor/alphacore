"""
Natural language instruction generation for Terraform tasks.

This module requires an OpenAI-compatible LLM client; if the client fails or
is unavailable the caller must handle the resulting RuntimeError.

Configuration via environment variables:
- OPENAI_API_KEY: API key (required; alias: ALPHACORE_OPENAI_API_KEY)
- OPENAI_BASE_URL: Base URL for local models (optional, e.g., http://localhost:11434/v1 for Ollama; alias: ALPHACORE_OPENAI_BASE_URL)
- ALPHACORE_TASK_PROMPT_MODEL: Model name (default: gpt-4o-mini; alias: ALPHACORE_LLM_MODEL)
- ALPHACORE_LLM_TEMPERATURE: Temperature (default: 0.6)
- ALPHACORE_LLM_RETRIES: Max retry attempts (default: 3)
- ALPHACORE_ENABLE_LLM: Enable/disable LLM (default: enabled when an API key is set)
- ALPHACORE_LLM_FALLBACK: Use deterministic fallback on failure (default: false)

See .env.example for configuration templates.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import random
import re
import textwrap
import time
from typing import Iterable, Optional

from modules.models import Invariant, TerraformTask

try:  # pragma: no cover - optional dependency
    from openai import OpenAI
except ImportError:  # pragma: no cover - optional dependency
    OpenAI = None  # type: ignore[assignment]

LOGGER = logging.getLogger(__name__)
DEFAULT_MODEL = os.getenv("ALPHACORE_TASK_PROMPT_MODEL", "gpt-5-mini")
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

# Many invariant values are stable identifiers (names, regions, IDs). Some are
# sentence-like descriptions that are easy for an LLM to paraphrase. For those,
# we only require that the prompt includes the stable identifier(s) embedded in
# the description (e.g., "82c280-6f6fd8"), not the entire sentence verbatim.
_IDENTIFIER_TOKEN_RE = re.compile(r"\b[a-z0-9]{4,}(?:-[a-z0-9]{3,})+\b")
_IGNORED_IDENTIFIER_TERMS = {"acore-token"}

# Disallowed term matching should be robust to common variants (plural forms and
# guideline(s)) while avoiding substring false positives (e.g. "guidance").
_DISALLOWED_TERM_PATTERNS: dict[str, re.Pattern[str]] = {
    "readme": re.compile(r"\breadmes?\b", flags=re.IGNORECASE),
    "prerequisite": re.compile(r"\bprerequisites?\b", flags=re.IGNORECASE),
    "documentation": re.compile(r"\bdocumentation\b", flags=re.IGNORECASE),
    "tutorial": re.compile(r"\btutorials?\b", flags=re.IGNORECASE),
    "guide": re.compile(r"\bguide(s)?\b|\bguideline(s)?\b", flags=re.IGNORECASE),
    "walkthrough": re.compile(r"\bwalkthroughs?\b", flags=re.IGNORECASE),
}

_UPPER_ENUM_TOKEN_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,}$")


class TaskInstructionGenerator:
    """
    Generates user-facing instructions for a Terraform task.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        enable_llm: Optional[bool] = None,
        llm_retries: Optional[int] = None,
        fallback_on_failure: Optional[bool] = None,
    ) -> None:
        """
        Initialize instruction generator with optional overrides.

        All parameters default to environment variables if not provided:

        Args:
            model: Model name (env: ALPHACORE_TASK_PROMPT_MODEL, default: gpt-4o-mini)
            temperature: Generation temperature (env: ALPHACORE_LLM_TEMPERATURE, default: 0.6)
            enable_llm: Enable LLM generation (env: ALPHACORE_ENABLE_LLM, default: enabled when an API key is set)
            llm_retries: Max retry attempts (env: ALPHACORE_LLM_RETRIES, default: 2)
            fallback_on_failure: Use deterministic fallback on error (env: ALPHACORE_LLM_FALLBACK, default: false)
        """
        # Helper to parse boolean environment variables
        def get_bool_env(key: str, default: bool) -> bool:
            val = os.getenv(key)
            if val is None:
                return default
            return val.lower() in ("true", "1", "yes", "on")

        # Load from environment with explicit parameter overrides
        api_key = os.getenv("OPENAI_API_KEY") or os.getenv("ALPHACORE_OPENAI_API_KEY")
        default_enable_llm = bool(api_key) and OpenAI is not None
        self.temperature = (
            temperature if temperature is not None
            else float(os.getenv("ALPHACORE_LLM_TEMPERATURE", "0.6"))
        )
        self.model = (
            model
            or os.getenv("ALPHACORE_TASK_PROMPT_MODEL")
            or os.getenv("ALPHACORE_LLM_MODEL")
            or DEFAULT_MODEL
        )
        self.enable_llm = (
            enable_llm if enable_llm is not None
            else get_bool_env("ALPHACORE_ENABLE_LLM", default_enable_llm)
        )
        self.llm_retries = (
            llm_retries if llm_retries is not None
            else int(os.getenv("ALPHACORE_LLM_RETRIES", "3"))
        )
        self.fallback_on_failure = (
            fallback_on_failure if fallback_on_failure is not None
            else get_bool_env("ALPHACORE_LLM_FALLBACK", False)
        )
        self.llm_retries = max(1, self.llm_retries)

        self.api_key = api_key
        self.base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("ALPHACORE_OPENAI_BASE_URL")
        self._client = None
        self._current_task: Optional[TerraformTask] = None
        self.last_trace: Optional[dict] = None
        self._log_prompt_text = get_bool_env("ALPHACORE_LOG_PROMPT_TEXT", False)

        # Control prompt post-processing. The generator always strips markdown/emoji
        # and enforces required terms, but additional sentence rewrites are optional.
        postprocess = (os.getenv("ALPHACORE_PROMPT_POSTPROCESS") or "full").strip().lower()
        if postprocess not in {"full", "minimal", "none"}:
            postprocess = "full"
        self._prompt_postprocess = postprocess

        # NEW: raw LLM response logging (opt-in).
        self._log_llm_raw = get_bool_env("ALPHACORE_LOG_LLM_RAW", False)
        try:
            preview = int(os.getenv("ALPHACORE_LOG_LLM_RAW_PREVIEW_CHARS", "900") or "900")
            # Always keep at least some preview so failure logs are actionable.
            self._log_llm_raw_preview_chars = max(200, preview)
        except Exception:
            self._log_llm_raw_preview_chars = 900
        try:
            self._log_llm_raw_max_chars = int(os.getenv("ALPHACORE_LOG_LLM_RAW_MAX_CHARS", "20000") or "20000")
        except Exception:
            self._log_llm_raw_max_chars = 20000

        self._llm_unsupported_params: dict[str, set[str]] = {}

    @property
    def client(self) -> Optional["OpenAI"]:
        """Lazy-loaded OpenAI client (only created when needed)."""
        if self._client is None and self.enable_llm and OpenAI is not None and self.api_key:
            try:
                client_kwargs = {"api_key": self.api_key}
                if self.base_url:
                    client_kwargs["base_url"] = self.base_url
                self._client = OpenAI(**client_kwargs)
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.warning("Failed to initialise OpenAI client: %s", exc)
                self._client = None
        return self._client

    @staticmethod
    def _max_output_tokens() -> int:
        """
        Max output tokens for the LLM prompt.

        This is intentionally read from the environment at call time so scripts
        (like taskgen_smoke) can override it without requiring a process restart.
        """
        raw = os.getenv("ALPHACORE_LLM_MAX_OUTPUT_TOKENS", "4096")
        try:
            value = int(raw or "4096")
        except Exception:
            value = 4096
        return max(200, min(int(value), 20000))

    def _llm_complete(
        self,
        client: "OpenAI",
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 400,
    ) -> tuple[str, Optional[dict]]:
        prefer = (os.getenv("ALPHACORE_LLM_API", "") or "").strip().lower()
        prefer_responses = prefer in {"responses", "response"}
        prefer_chat = prefer in {"chat", "completions", "chat_completions", "chat-completions"}
        model_lower = (self.model or "").lower()
        client_module = getattr(type(client), "__module__", "") or ""
        is_openai_sdk_client = client_module.startswith("openai")

        last_exc: Optional[Exception] = None
        model_key = (self.model or "").strip().lower()
        unsupported = self._llm_unsupported_params.setdefault(model_key, set())

        def _is_unsupported_param(exc: Exception, name: str) -> bool:
            msg = str(exc).lower()
            needle = (name or "").strip().lower()
            if not needle:
                return False
            return (
                "unsupported parameter" in msg
                and (f"'{needle}'" in msg or f"\"{needle}\"" in msg or f"param': '{needle}'" in msg)
            )

        def _usage_payload_from_usage(usage: object) -> Optional[dict]:
            if usage is None:
                return None

            def _coerce_int(value: object) -> int:
                if value is None:
                    return 0
                try:
                    return int(value)  # type: ignore[arg-type]
                except Exception:
                    return 0

            # chat.completions usage
            if hasattr(usage, "prompt_tokens") or hasattr(usage, "completion_tokens"):
                return {
                    "prompt_tokens": _coerce_int(getattr(usage, "prompt_tokens", 0) or 0),
                    "completion_tokens": _coerce_int(getattr(usage, "completion_tokens", 0) or 0),
                    "total_tokens": _coerce_int(getattr(usage, "total_tokens", 0) or 0),
                }
            # responses usage
            if hasattr(usage, "input_tokens") or hasattr(usage, "output_tokens"):
                return {
                    "prompt_tokens": _coerce_int(getattr(usage, "input_tokens", 0) or 0),
                    "completion_tokens": _coerce_int(getattr(usage, "output_tokens", 0) or 0),
                    "total_tokens": _coerce_int(getattr(usage, "total_tokens", 0) or 0),
                }
            return None

        def _responses() -> tuple[str, Optional[dict]]:
            kwargs = dict(
                model=self.model,
                input=user_prompt,
                instructions=system_prompt,
                max_output_tokens=20000,
            )
            if "temperature" not in unsupported:
                kwargs["temperature"] = self.temperature
            try:
                response = client.responses.create(**kwargs)
            except Exception as exc:
                if _is_unsupported_param(exc, "temperature"):
                    unsupported.add("temperature")
                    kwargs.pop("temperature", None)
                    response = client.responses.create(**kwargs)
                else:
                    raise
            usage_payload = _usage_payload_from_usage(getattr(response, "usage", None))
            text = (getattr(response, "output_text", None) or "").strip()
            if not text:
                # Best-effort fallback: some SDK versions expose `output` items instead of `output_text`.
                output = getattr(response, "output", None)
                if isinstance(output, list):
                    chunks: list[str] = []
                    for item in output:
                        content = getattr(item, "content", None)
                        if not isinstance(content, list):
                            continue
                        for piece in content:
                            chunk = getattr(piece, "text", None)
                            if isinstance(chunk, str) and chunk:
                                chunks.append(chunk)
                    text = "\n".join(chunks).strip()
            return text, usage_payload

        def _chat() -> tuple[str, Optional[dict]]:
            common_kwargs = dict(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                n=1,
            )
            if "temperature" not in unsupported:
                common_kwargs["temperature"] = self.temperature
            # Some newer models reject `max_tokens` and require `max_completion_tokens`.
            try:
                response = client.chat.completions.create(
                    **common_kwargs,
                    max_completion_tokens=max_tokens,
                )
            except Exception as exc:
                msg = str(exc).lower()
                if _is_unsupported_param(exc, "temperature"):
                    unsupported.add("temperature")
                    common_kwargs.pop("temperature", None)
                    response = client.chat.completions.create(
                        **common_kwargs,
                        max_completion_tokens=max_tokens,
                    )
                elif "unsupported parameter" in msg and "max_tokens" in msg:
                    response = client.chat.completions.create(
                        **common_kwargs,
                        max_completion_tokens=max_tokens,
                    )
                elif "max_completion_tokens" in msg and ("unsupported" in msg or "unknown" in msg):
                    response = client.chat.completions.create(
                        **common_kwargs,
                        max_tokens=max_tokens,
                    )
                else:
                    # Heuristic: for gpt-5* default to max_completion_tokens; otherwise retry with max_tokens.
                    if model_lower.startswith("gpt-5"):
                        response = client.chat.completions.create(
                            **common_kwargs,
                            max_completion_tokens=max_tokens,
                        )
                    else:
                        response = client.chat.completions.create(
                            **common_kwargs,
                            max_tokens=max_tokens,
                        )
            usage_payload = _usage_payload_from_usage(getattr(response, "usage", None))
            message = response.choices[0].message.content if getattr(response, "choices", None) else ""
            return (message or "").strip(), usage_payload

        # Try preferred API first; fall back to the other API on errors that
        # commonly occur when a model is only available on one endpoint.
        if prefer_responses:
            api_order = ["responses", "chat"]
        else:
            # Default to chat for maximum compatibility; fall back to responses if needed.
            api_order = ["chat", "responses"]
            if prefer_chat:
                api_order = ["chat", "responses"]
        for api in api_order:
            try:
                if api == "responses" and hasattr(client, "responses") and (prefer_responses or is_openai_sdk_client):
                    return _responses()
                if api == "chat" and hasattr(client, "chat"):
                    return _chat()
            except Exception as exc:
                last_exc = exc
                msg = str(exc).lower()
                # If it doesn't look like an endpoint mismatch, don't silently switch APIs.
                if "unsupported" not in msg and "responses" not in msg and "endpoint" not in msg and "not found" not in msg:
                    raise

        if last_exc:
            raise last_exc
        raise RuntimeError("LLM call failed: no supported API available on client.")

    def generate(self, task: TerraformTask, task_name: Optional[str] = None) -> str:
        """
        Create miner-facing instructions describing the task.
        """
        # If LLM is disabled, use fallback instructions directly
        if not self.enable_llm:
            started = time.time()
            fallback = self._fallback_instructions(task)
            if self._prompt_postprocess == "full":
                fallback = self._normalize_prompt_phrasing(fallback, task)
            elif self._prompt_postprocess == "minimal":
                fallback = self._normalize_submission_instructions(fallback, task)
            fallback = self._downcase_invariant_enum_tokens(fallback, task)
            fallback = self._ensure_provider_reference(fallback, task)
            fallback = re.sub(r"\s+", " ", fallback).strip()
            validated = self._enforce_allowed_content(fallback, task)
            try:
                self.last_trace = {
                    "task_id": getattr(task.spec, "task_id", "") or "",
                    "task_kind": getattr(task.spec, "kind", "") or "",
                    "provider": getattr(task, "provider", ""),
                    "model": None,
                    "temperature": None,
                    "retries": 0,
                    "fallback_on_failure": bool(self.fallback_on_failure),
                    "start_ts": float(started),
                    "duration_s": float(time.time() - started),
                    "attempts": [],
                    "success": True,
                    "fallback_used": True,
                    "final_attempt": 0,
                    "error": None,
                }
            except Exception:
                self.last_trace = None
            return validated

        context = self._build_context(task, task_name)
        attribute_order_hint = self._attribute_order_hint(task)
        style_directive = self._style_directive()

        # Prefer an explicitly injected client (tests) over lazy initialisation.
        client = self._client or self.client
        if not client:
            try:
                self.last_trace = {
                    "task_id": getattr(task.spec, "task_id", "") or "",
                    "task_kind": getattr(task.spec, "kind", "") or "",
                    "provider": getattr(task, "provider", ""),
                    "model": self.model,
                    "temperature": self.temperature,
                    "retries": self.llm_retries,
                    "fallback_on_failure": bool(self.fallback_on_failure),
                    "start_ts": time.time(),
                    "duration_s": 0.0,
                    "attempts": [],
                    "success": False,
                    "fallback_used": False,
                    "final_attempt": None,
                    "error": {"type": "RuntimeError", "message": "OpenAI client unavailable"},
                }
            except Exception:
                self.last_trace = None
            if not self.api_key:
                raise RuntimeError(
                    "LLM instruction generator requested but OpenAI client is unavailable "
                    "(missing OPENAI_API_KEY / ALPHACORE_OPENAI_API_KEY). Set ALPHACORE_ENABLE_LLM=false "
                    "to use the deterministic fallback."
                )
            raise RuntimeError("LLM instruction generator requested but OpenAI client is unavailable.")

        task_id = getattr(task.spec, "task_id", "") or ""
        task_kind = getattr(task.spec, "kind", "") or ""
        trace = {
            "task_id": task_id,
            "task_kind": task_kind,
            "provider": getattr(task, "provider", ""),
            "model": self.model,
            "temperature": self.temperature,
            "retries": self.llm_retries,
            "fallback_on_failure": bool(self.fallback_on_failure),
            "start_ts": time.time(),
            "duration_s": None,
            "attempts": [],
            "success": False,
            "fallback_used": False,
            "final_attempt": None,
            "error": None,
        }
        LOGGER.info(
            "PROMPT_GEN_START %s",
            json.dumps(
                {
                    "task_id": task_id,
                    "task_kind": task_kind,
                    "provider": getattr(task, "provider", ""),
                    "model": self.model,
                    "temperature": self.temperature,
                    "retries": self.llm_retries,
                },
                ensure_ascii=True,
            ),
        )

        system_prompt = (
            "You write short infrastructure requests for expert Terraform engineers. "
            "Write like a real internal ticket: brief context, then what to provision. "
            "Include all pinned identifiers and values somewhere in the text, but do NOT present them as a rigid "
            "'field equals value' specification. Prefer outcome phrasing. "
            "When mentioning enum-like configuration values (e.g., formats, storage classes, directions), prefer lowercase words in the narrative. "
            "Do not start with policy-like lines such as 'All resources must…'. "
            "Avoid lecturing tone and avoid repeating 'Ensure…' every sentence. "
            "Avoid phrases like 'Pin the resource' and avoid listing raw attribute/value pairs; write natural sentences instead. "
            "Mention submission expectations (use the literal word 'zip'; terraform.tfstate at the repository root) in exactly ONE sentence, without describing Terraform CLI steps. "
            "Do not repeat or paraphrase submission details elsewhere in the prompt. "
            "Do not refer to validators, scoring, or state inspection; phrase access as 'Grant <principal> viewer access so they can verify the deployment'. "
            "Avoid filler like 'confirm that terraform.tfstate is located at terraform.tfstate'; say 'include terraform.tfstate at the repository root in the submission'. "
            "Do not include greetings, thanks, or sign-offs. "
            "Respond using only plain ASCII text; no markdown, no bullets, no numbered lists, no emojis. "
            "Avoid using the word 'invariant' and never introduce deliverables beyond the provided resource specs and "
            "submission instructions (for example, do not mention README files). "
            "Do not use these words anywhere in the response: README, prerequisite, documentation, tutorial, guide, walkthrough."
        )
        validator_sa = (getattr(task, "validator_sa", None) or "").strip()
        verifier_sentence = (
            f"Grant {validator_sa} viewer access so they can verify the deployment."
            if validator_sa
            else ""
        )
        submission_variants = [
            "Submit a single zip archive of the repository; keep the Terraform config at the repository root and include terraform.tfstate at the repository root.",
            "Bundle the repository into one zip archive for submission; keep Terraform at the repository root and include terraform.tfstate at the repository root.",
            "Deliver one zip archive containing the repository with the Terraform config at the repository root and terraform.tfstate at the repository root.",
        ]
        pinned_terms = list(self._pinned_terms_for_llm(task))
        random.shuffle(pinned_terms)
        # Present tokens comma-separated to discourage raw pipe-delimited dumps in the final prompt.
        pinned_terms_str = ", ".join(pinned_terms)
        base_user_prompt = textwrap.dedent(
            f"""
            Compose miner instructions for the following task metadata.

            {context}

            Requirements:
            - Write as a human request (not a validator spec); keep it concise and practical.
            - The text must still include every pinned identifier/value implied by the resource requirements so an implementer can follow it exactly.
            - Include packaging expectations using the literal word 'zip' and referencing terraform.tfstate at the repository root.
            - Rephrase the guidance each time, using varied sentence structures and shuffling the order of key details so that no two prompts share the same template.
            - Follow this style directive: {style_directive}
            - Follow this attribute cadence when describing resource properties: {attribute_order_hint}
            - Keep it under 220 words and prefer imperative voice.
            - Produce plain sentences with no markdown or special formatting characters.
            - Do not add requirements or artefacts beyond what is described above.

            Hard constraint:
            - Weave each of these tokens naturally into the sentences (do not group or list them; spread them across the prose): {pinned_terms_str}

            Verbatim sentences (include each sentence exactly once):
            - {verifier_sentence}
            - Choose ONE of the following submission sentences and include it verbatim:
              - {submission_variants[0]}
              - {submission_variants[1]}
              - {submission_variants[2]}
            """
        ).strip()

        last_error: Optional[Exception] = None
        self._current_task = task
        extra_requirements: list[str] = []
        try:
            for attempt in range(self.llm_retries):
                attempt_started = time.time()

                # NEW: capture raw model output per attempt (for logging + trace on failures).
                raw_message: str = ""
                raw_sha256: Optional[str] = None
                raw_preview: Optional[str] = None
                usage_payload: Optional[dict] = None

                LOGGER.info(
                    "PROMPT_GEN_ATTEMPT %s",
                    json.dumps(
                        {
                            "task_id": task_id,
                            "task_kind": task_kind,
                            "attempt": attempt + 1,
                            "retries": self.llm_retries,
                        },
                        ensure_ascii=True,
                    ),
                )
                try:
                    user_prompt = base_user_prompt
                    if extra_requirements:
                        extras = "\n".join(extra_requirements)
                        user_prompt = f"{base_user_prompt}\n\nAdditional reminders:\n{extras}"

                    message, usage_payload = self._llm_complete(
                        client,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        max_tokens=self._max_output_tokens(),
                    )

                    # NEW: raw capture before any transformations/filters.
                    raw_message = message or ""
                    if raw_message:
                        raw_sha256 = hashlib.sha256(raw_message.encode("utf-8")).hexdigest()
                        raw_preview = raw_message[: self._log_llm_raw_preview_chars]

                    cleaned = self._to_plain_text(raw_message)
                    cleaned, auto_fixed_startup_shebang = self._repair_startup_shebang(cleaned, task)
                    if self._prompt_postprocess == "full":
                        cleaned = self._normalize_prompt_phrasing(cleaned, task)
                    elif self._prompt_postprocess == "minimal":
                        cleaned = self._normalize_submission_instructions(cleaned, task)
                    cleaned = self._downcase_invariant_enum_tokens(cleaned, task)
                    cleaned = self._ensure_provider_reference(cleaned, task)
                    cleaned = re.sub(r"\s+", " ", cleaned).strip()
                    auto_fixed_disallowed_terms: list[str] = []
                    usage_total_tokens_all_attempts = 0
                    try:
                        if usage_payload:
                            usage_total_tokens_all_attempts += int(usage_payload.get("total_tokens", 0) or 0)
                    except Exception:
                        pass
                    try:
                        validated = self._enforce_allowed_content(cleaned, task)
                    except Exception as exc:
                        # If the only issue is a disallowed term, replace it deterministically
                        # with a neutral synonym and re-validate without consuming another retry.
                        disallowed = self._disallowed_term_from_error(exc)
                        if disallowed:
                            repaired, replaced = self._replace_disallowed_terms(cleaned, {disallowed})
                            if replaced:
                                validated = self._enforce_allowed_content(repaired, task)
                                auto_fixed_disallowed_terms.extend(sorted(replaced))
                            else:
                                raise
                        else:
                            raise
                    trace["success"] = True
                    trace["final_attempt"] = int(attempt + 1)
                    trace["attempt_duration_s"] = float(time.time() - attempt_started)
                    trace["usage"] = usage_payload
                    trace["auto_fixed_startup_shebang"] = bool(auto_fixed_startup_shebang)
                    trace["auto_fixed_disallowed_terms"] = list(auto_fixed_disallowed_terms)

                    # NEW: include raw fingerprints for debugging (and optionally full raw).
                    if raw_sha256:
                        trace["llm_raw_sha256"] = raw_sha256
                    if raw_preview:
                        trace["llm_raw_preview"] = raw_preview
                    if self._log_llm_raw and raw_message:
                        trace["llm_raw"] = raw_message[: max(0, int(self._log_llm_raw_max_chars))]

                    prompt_preview = validated[:240]
                    prompt_sha256 = hashlib.sha256(validated.encode("utf-8")).hexdigest()
                    success_payload = {
                        "task_id": task_id,
                        "task_kind": task_kind,
                        "attempt": attempt + 1,
                        "attempt_duration_s": float(time.time() - attempt_started),
                        "total_duration_s": float(time.time() - float(trace["start_ts"])),
                        "chars": len(validated),
                        "prompt_preview": prompt_preview,
                        "prompt_sha256": prompt_sha256,
                        "auto_fixed_startup_shebang": bool(auto_fixed_startup_shebang),
                        "auto_fixed_disallowed_terms": auto_fixed_disallowed_terms,
                    }
                    if usage_payload:
                        success_payload["usage"] = usage_payload

                    # NEW: add raw preview/hash to success logs; full raw only if enabled.
                    if raw_sha256:
                        success_payload["llm_raw_sha256"] = raw_sha256
                    if raw_preview:
                        success_payload["llm_raw_preview"] = raw_preview
                    if self._log_llm_raw and raw_message:
                        success_payload["llm_raw"] = raw_message[: max(0, int(self._log_llm_raw_max_chars))]

                    if self._log_prompt_text:
                        success_payload["prompt"] = validated

                    LOGGER.info(
                        "PROMPT_GEN_SUCCESS %s",
                        json.dumps(
                            success_payload,
                            ensure_ascii=True,
                        ),
                    )
                    return validated
                except Exception as exc:  # pragma: no cover - network failure fallback
                    last_error = exc
                    missing = self._missing_detail_from_error(exc)
                    missing_origin = self._missing_detail_origin(task, missing) if missing else None
                    if missing:
                        reminder = f"Include the exact token: {missing}."
                        if reminder not in extra_requirements:
                            extra_requirements.append(reminder)
                    disallowed = self._disallowed_term_from_error(exc)
                    if disallowed:
                        reminder = f"Do not use the word: {disallowed}."
                        if reminder not in extra_requirements:
                            extra_requirements.append(reminder)

                    # NEW: structured failure payload including raw output preview/hash.
                    failure_payload = {
                        "task_id": task_id,
                        "task_kind": task_kind,
                        "attempt": attempt + 1,
                        "retries": self.llm_retries,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "missing_detail": missing,
                        "missing_detail_origin": missing_origin,
                        "disallowed_term": disallowed,
                        "reminders": list(extra_requirements),
                    }
                    if raw_sha256:
                        failure_payload["llm_raw_sha256"] = raw_sha256
                    if raw_preview:
                        failure_payload["llm_raw_preview"] = raw_preview
                    if self._log_llm_raw and raw_message:
                        failure_payload["llm_raw"] = raw_message[: max(0, int(self._log_llm_raw_max_chars))]
                    if usage_payload:
                        failure_payload["usage"] = usage_payload

                    try:
                        trace["attempts"].append(
                            {
                                "attempt": int(attempt + 1),
                                "attempt_duration_s": float(time.time() - attempt_started),
                                "usage": usage_payload,
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                                "missing_detail": missing,
                                "missing_detail_origin": missing_origin,
                                "disallowed_term": disallowed,
                                "reminders": list(extra_requirements),
                                "llm_raw_sha256": raw_sha256,
                                "llm_raw_preview": raw_preview,
                                **(
                                    {
                                        "llm_raw": raw_message[: max(0, int(self._log_llm_raw_max_chars))]
                                    }
                                    if (self._log_llm_raw and raw_message)
                                    else {}
                                ),
                            }
                        )
                    except Exception:
                        pass

                    LOGGER.warning(
                        "PROMPT_GEN_FAILURE %s",
                        json.dumps(failure_payload, ensure_ascii=True),
                    )

            # All retries failed
            if self.fallback_on_failure:
                LOGGER.warning(
                    "PROMPT_GEN_FALLBACK %s",
                    json.dumps(
                        {
                            "task_id": task_id,
                            "task_kind": task_kind,
                            "retries": self.llm_retries,
                            "last_error_type": type(last_error).__name__ if last_error else None,
                            "last_error": str(last_error) if last_error else None,
                        },
                        ensure_ascii=True,
                    ),
                )
                fallback = self._fallback_instructions(task)
                fallback = self._ensure_provider_reference(fallback, task)
                validated = self._enforce_allowed_content(fallback, task)
                trace["fallback_used"] = True
                trace["final_attempt"] = int(self.llm_retries)
                LOGGER.info(
                    "PROMPT_GEN_FALLBACK_PROMPT %s",
                    json.dumps(
                        (
                            {
                                "task_id": task_id,
                                "task_kind": task_kind,
                                "chars": len(validated),
                                "prompt_preview": validated[:240],
                                "prompt_sha256": hashlib.sha256(validated.encode("utf-8")).hexdigest(),
                                **({"prompt": validated} if self._log_prompt_text else {}),
                            }
                        ),
                        ensure_ascii=True,
                    ),
                )
                return validated
            else:
                raise RuntimeError(
                    "LLM instruction generation failed after "
                    f"{self.llm_retries} attempts."
                ) from last_error
        finally:
            try:
                trace["duration_s"] = float(time.time() - float(trace["start_ts"]))
                if last_error and not trace.get("success"):
                    trace["error"] = {"type": type(last_error).__name__, "message": str(last_error)}
                self.last_trace = trace
            except Exception:
                self.last_trace = None
            self._current_task = None

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _build_context(self, task: TerraformTask, task_name: Optional[str]) -> str:
        spec = task.spec
        requirements = self._format_invariants(spec.invariants)
        kind_description = self._describe_kind(spec.kind)
        metadata = spec.metadata or {}
        hints = metadata.get("hints") or []
        submission_details = self._format_submission_details(task)
        background = self._background_context(task)
        context_blocks = [
            ("Provider", self._provider_label(task)),
            ("Background", background),
            ("Resource kind", kind_description),
            ("Resource requirements", requirements or "None"),
            ("Submission details", submission_details),
        ]
        if task.validator_sa:
            context_blocks.append(("Verification principal", task.validator_sa))
        if hints:
            hints_body = "\n".join(hints)
            context_blocks.append(("Design cues", hints_body))
        random.shuffle(context_blocks)
        formatted = [f"{title}:\n{body}" for title, body in context_blocks]
        return "\n\n".join(formatted)

    @staticmethod
    def _background_context(task: TerraformTask) -> str:
        """
        Soft context to reduce templated, policy-like prompts.

        This is intentionally non-binding: it should not introduce extra resources
        or new hard requirements beyond the invariants.
        """
        options = [
            "Internal dev sandbox. Keep it simple and cost-conscious; no extras beyond what is requested.",
            "Small internal setup for a teammate. Prefer minimal surface area and predictable configuration.",
            "Lightweight staging setup. Keep access conservative (least privilege) and avoid optional add-ons.",
            "Quick validation run. Prioritize fast apply/destroy and keep resources minimal.",
        ]
        seed = getattr(task.spec, "task_id", "") or "alphacore"
        return random.Random(seed).choice(options)

    def _ensure_validator_access_line(self, text: str, task: TerraformTask) -> str:
        """
        Ensure the prompt mentions the validator service account for IAM access.

        This is a mandatory requirement for miners; we patch it in deterministically
        when the LLM otherwise produces a good prompt but forgets the service account.
        """
        validator_sa = (getattr(task, "validator_sa", None) or "").strip()
        if not validator_sa:
            return text
        if self._contains_required_detail(text, validator_sa):
            return text
        suffix = (
            f" Grant {validator_sa} viewer access so they can verify the deployment."
        )
        # Keep this on the same line to avoid changing output structure too much.
        return (text.rstrip() + suffix).strip()

    @staticmethod
    def _repair_startup_shebang(text: str, task: TerraformTask) -> tuple[str, bool]:
        """
        Ensure the shebang line for startup scripts is not accidentally mangled.

        LLM outputs occasionally drop the leading `#` (e.g. `!/bin/bash`). This is
        harmless for many readers but breaks exact startup-script invariants and
        can mislead miners. We repair it deterministically when present.
        """
        # Only attempt repair if the task actually contains a startup-script invariant.
        has_startup_script = False
        for invariant in getattr(task.spec, "invariants", []) or []:
            match = getattr(invariant, "match", None) or {}
            if not isinstance(match, dict):
                continue
            for field in match.keys():
                field_lower = (str(field) or "").lower()
                if "metadata_startup_script" in field_lower:
                    has_startup_script = True
                    break
            if has_startup_script:
                break
        if not has_startup_script:
            return text, False

        pattern = re.compile(r'(^|["\\s])!/bin/bash')
        repaired = pattern.sub(r"\1#!/bin/bash", text)
        return repaired, repaired != text

    @staticmethod
    def _replace_disallowed_terms(text: str, terms: set[str]) -> tuple[str, set[str]]:
        """
        Replace disallowed terms with neutral synonyms (best-effort).

        This is used as a small auto-repair step when the LLM output is otherwise valid.
        """
        replacements = {
            "guide": "instructions",
            "tutorial": "instructions",
            "walkthrough": "instructions",
            "documentation": "instructions",
            "prerequisite": "requirement",
            "readme": "notes",
        }
        variant_patterns: dict[str, re.Pattern[str]] = {
            "guide": re.compile(r"\bguide(s)?\b|\bguideline(s)?\b", flags=re.IGNORECASE),
            "tutorial": re.compile(r"\btutorials?\b", flags=re.IGNORECASE),
            "walkthrough": re.compile(r"\bwalkthroughs?\b", flags=re.IGNORECASE),
            "documentation": re.compile(r"\bdocumentation\b", flags=re.IGNORECASE),
            "prerequisite": re.compile(r"\bprerequisites?\b", flags=re.IGNORECASE),
            "readme": re.compile(r"\breadmes?\b", flags=re.IGNORECASE),
        }
        replaced: set[str] = set()
        out = text
        for term in sorted(terms):
            key = (term or "").strip().lower()
            if not key:
                continue
            repl = replacements.get(key)
            if not repl:
                continue
            # Variant-aware replacement.
            pattern = variant_patterns.get(key) or re.compile(
                rf"\\b{re.escape(key)}\\b", flags=re.IGNORECASE
            )
            if pattern.search(out):
                out = pattern.sub(repl, out)
                replaced.add(key)
        return out, replaced

    def _fallback_instructions(self, task: TerraformTask) -> str:
        submission_details = self._format_submission_details(task)
        requirements_summary = self._summarize_invariants(task.spec.invariants)
        kind_description = self._describe_kind(task.spec.kind)
        metadata = task.spec.metadata or {}
        hints = metadata.get("hints") or []
        validator_notice = ""
        if task.validator_sa and not self._contains_required_detail(requirements_summary, task.validator_sa):
            validator_notice = f"Grant {task.validator_sa} viewer access so they can verify the deployment."
        provider_label = self._provider_label(task)
        background = self._background_context(task)
        intro = f"{background} We need the following set up in {provider_label}."
        scope = "Please only create what's described below (no extra resources)."
        parts = [intro, f"Requested: {kind_description}.", scope, requirements_summary, submission_details]
        if validator_notice:
            parts.append(validator_notice)
        if hints:
            cues = " ".join(hints)
            parts.append(cues)
        text = "\n\n".join(parts).strip()
        return text

    @staticmethod
    def _normalize_prompt_phrasing(text: str, task: TerraformTask) -> str:
        """
        Deterministic phrasing cleanup to keep prompts natural.

        This intentionally targets a small set of repetitive phrases that appear
        in LLM outputs and makes them read more like an internal request without
        changing required terms.
        """
        if not text:
            return text

        out = text
        out = re.sub(
            r"(?i)\bconfirm( that)? terraform\.tfstate (is )?located( correctly)? (at|in)\s+terraform\.tfstate\b\.?",
            "Include terraform.tfstate at the repository root in the submission.",
            out,
        )
        out = re.sub(
            r"(?i)\ballowing them to (inspect|review) the state as needed\b\.?",
            "so they can verify the deployment.",
            out,
        )
        out = re.sub(
            r"(?i)\binspect the state\b\.?",
            "verify the deployment.",
            out,
        )
        out = re.sub(r"(?i)\bthis access will enable[^.]*\.\s*", "", out)
        out = re.sub(r"(?i)\bfollowing these instructions[^.]*\.\s*", "", out)
        out = re.sub(r"(?i)\bthis will help[^.]*\.\s*", "", out)
        out = re.sub(
            r"(?i)\bexplicit message retention duration set to\b",
            "message retention duration of",
            out,
        )
        out = re.sub(
            r"(?i)\bthis topic should be isolated\b[^.]*\.",
            "Keep the topic standalone (no extra resources).",
            out,
        )
        out = re.sub(
            r"(?i)\b(records?|documents?) the (repository )?description\b",
            "notes the purpose",
            out,
        )
        out = re.sub(
            r"(?i)\bnotes the purpose and format\b",
            "notes the purpose and package format",
            out,
        )

        # Strip mail-like "Subject:" headers if the model produces them.
        out = re.sub(r"(?im)^\s*subject:\s*[^\n]*\n+", "", out).strip()

        # Avoid meta context phrasing that reads synthetic in batches.
        out = re.sub(r"(?i)\b(one[- ]off|smoke test|proof[- ]of[- ]concept)\b", "internal", out)

        principal = (getattr(task, "validator_sa", None) or "").strip()
        if principal:
            out = re.sub(
                r"(?i)\bprovide read access to the validator at\s+" + re.escape(principal) + r"\b",
                f"Grant {principal} viewer access",
                out,
            )
            out = re.sub(
                r"(?i)\bprovide read access to the validator at\b",
                "Grant viewer access to",
                out,
            )
            out = re.sub(
                r"(?i)\bprovide read access to\s+" + re.escape(principal) + r"\b",
                f"Grant {principal} viewer access",
                out,
            )

        # Avoid calling the reviewer a "validator" in narrative text, but do not
        # rewrite tokens that look like they are part of an email/local-part such as
        # `alpha-core-validator-1@...` or `validator@...`.
        out = re.sub(r"(?i)\bvalidator\b(?![-@.])", "reviewer", out)

        # Remove common polite sign-offs that make prompts sound synthetic.
        out = re.sub(r"(?is)\s*(thank you[^\n.]*[.?!]\s*)+$", "", out).strip()
        out = re.sub(r"(?is)\s*(i look forward to[^.]*[.?!]\s*)+$", "", out).strip()
        out = re.sub(r"(?is)\s*(let me know if you need[^.]*[.?!]\s*)+$", "", out).strip()

        out = TaskInstructionGenerator._normalize_submission_instructions(out, task)
        out = re.sub(r"\s+", " ", out).strip()
        return out.strip()

    @staticmethod
    def _downcase_invariant_enum_tokens(text: str, task: TerraformTask) -> str:
        """
        Prefer lowercase for enum-like invariant values in the final prompt.

        We keep invariants themselves in canonical form for validation (often uppercase),
        but miner-facing text reads better in lowercase (e.g., "python" vs "PYTHON").
        """
        if not text:
            return text

        tokens: set[str] = set()
        for invariant in getattr(getattr(task, "spec", None), "invariants", []) or []:
            match = getattr(invariant, "match", None) or {}
            if not isinstance(match, dict):
                continue
            for value in match.values():
                if not isinstance(value, str):
                    continue
                candidate = value.strip()
                if not candidate or len(candidate) < 3:
                    continue
                if "@" in candidate or "." in candidate or "/" in candidate:
                    continue
                if _UPPER_ENUM_TOKEN_RE.fullmatch(candidate) and any(ch.isalpha() for ch in candidate):
                    tokens.add(candidate)

        out = text
        for token in sorted(tokens, key=len, reverse=True):
            escaped = re.escape(token)
            # Avoid `\b` because hyphens are non-word chars and can split tokens like `US-CENTRAL1`.
            pattern = re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", flags=re.IGNORECASE)
            out = pattern.sub(token.lower(), out)
        return out

    @staticmethod
    def _submission_sentence(task: TerraformTask) -> str:
        submit = task.to_dict().get("submit_requirements", {})
        layout = submit.get("bundle_layout", {"state": "terraform.tfstate"})
        state_path = layout.get("state", "terraform.tfstate") or "terraform.tfstate"
        state_is_root = state_path.strip("./") == "terraform.tfstate"
        state_clause = "terraform.tfstate at the repository root" if state_is_root else f"terraform.tfstate at {state_path}"
        variants = [
            f"Submit a single zip archive of the repository; keep the Terraform config at the repository root and include {state_clause}.",
            f"Bundle the repository into one zip archive for submission; keep Terraform at the repository root and include {state_clause}.",
            (
                "Deliver one zip archive containing the repository with the Terraform config at the repository root "
                + ("and terraform.tfstate at the repository root." if state_is_root else f"and terraform.tfstate at {state_path}.")
            ),
        ]
        seed = getattr(getattr(task, 'spec', None), 'task_id', '') or getattr(getattr(task, 'spec', None), 'nonce', '') or "alphacore"
        chooser = random.Random(seed)
        return chooser.choice(variants)

    @staticmethod
    def _normalize_submission_instructions(text: str, task: TerraformTask) -> str:
        """
        Replace overly-verbose or repeated packaging/state language with one sentence.

        This keeps prompts short and avoids repeating the same submission details
        in multiple paraphrases, while preserving required keywords (zip/archive,
        repository root, terraform.tfstate).
        """
        if not text:
            return text

        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        kept: list[str] = []
        saw_submission_sentence = False
        for sentence in sentences:
            lowered = sentence.lower()
            mentions_package = any(term in lowered for term in ("zip", "archive", "bundle", "package"))
            mentions_state = "terraform.tfstate" in lowered or "tfstate" in lowered
            mentions_root = "repository root" in lowered or "repo root" in lowered
            mentions_submission = "submission" in lowered
            is_submission_sentence = mentions_package and (mentions_state or mentions_root or mentions_submission)
            if is_submission_sentence:
                if saw_submission_sentence:
                    continue
                saw_submission_sentence = True
            kept.append(sentence)

        return " ".join(s.strip() for s in kept if s.strip()).strip()

    @staticmethod
    def _format_invariants(invariants: list[Invariant]) -> str:
        lines = []
        for invariant in invariants:
            shuffled_fields = list(invariant.match.items())
            random.shuffle(shuffled_fields)

            def _display_value(value: object) -> object:
                if not isinstance(value, str):
                    return value
                raw = value.strip()
                if not raw:
                    return value
                if "@" in raw or "." in raw or "/" in raw:
                    return value
                if _UPPER_ENUM_TOKEN_RE.fullmatch(raw) and any(ch.isalpha() for ch in raw):
                    return raw.lower()
                return value

            fields = ", ".join(
                f"{TaskInstructionGenerator._humanize_field(k)} equals {json.dumps(_display_value(v), ensure_ascii=True)}"
                for k, v in shuffled_fields
            )
            lines.append(fields)
        return "\n".join(lines) if lines else "No requirement details provided."

    @staticmethod
    def _format_submission_details(task: TerraformTask) -> str:
        return TaskInstructionGenerator._submission_sentence(task)

    @staticmethod
    def _humanize_field(field: str) -> str:
        parts = (field or "").split(".")
        if not parts:
            return ""
        stripped = parts[-1]
        # Some invariant paths include array indices (e.g. `ports.0`) which are
        # unhelpful in human text. Use the prior segment when possible.
        if stripped.isdigit() and len(parts) >= 2:
            stripped = parts[-2]
        return stripped.replace("_", " ")

    @staticmethod
    def _describe_kind(kind: str) -> str:
        if not kind:
            return "resource"
        if "." not in kind:
            # Many tasks use already-human labels like "storage bucket" or
            # "pubsub topic + storage bucket". Keep them as-is (no uppercasing).
            return re.sub(r"\s+", " ", kind.strip())
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
    def _style_directive() -> str:
        directives = [
            "Write it like a short internal ticket from an engineer (plain language, not policy text).",
            "Start with 1 sentence of context (why/where), then describe what to create in a few sentences.",
            "Avoid ‘All resources must…’ and avoid lecturing tone; be concise and practical.",
            "Avoid repeating ‘Ensure…’ for every sentence; mix sentence structure naturally.",
            "Prefer outcome phrasing (‘stand up a bucket’, ‘attach the VM to subnet …’) over field-by-field spec.",
        ]
        return random.choice(directives)

    def _attribute_order_hint(self, task: TerraformTask) -> str:
        hints = []
        for invariant in task.spec.invariants:
            keys = list(invariant.match.keys())
            if len(keys) < 2:
                continue
            random.shuffle(keys)
            humanized = [self._humanize_field(key) for key in keys]
            hints.append(self._join_clauses(humanized))
        if not hints:
            return "mix any remaining fields freely"
        return "; then ".join(hints)

    @staticmethod
    def _summarize_invariants(invariants: list[Invariant]) -> str:
        sentences = [
            TaskInstructionGenerator._summarize_invariant(invariant)
            for invariant in invariants
        ]
        return (
            " ".join(sentences).strip()
            or "No explicit attribute constraints were provided."
        )

    @staticmethod
    def _summarize_invariant(invariant: Invariant) -> str:
        shuffled_fields = list(invariant.match.items())
        random.shuffle(shuffled_fields)
        clauses = [
            f"{TaskInstructionGenerator._humanize_field(key)} is {json.dumps(value, ensure_ascii=True)}"
            for key, value in shuffled_fields
        ]
        if not clauses:
            return "Keep the resource minimal; no attributes are pinned."
        summary = TaskInstructionGenerator._join_clauses(clauses)
        return f"Pin the resource so {summary}."

    @staticmethod
    def _join_clauses(clauses: list[str]) -> str:
        if not clauses:
            return ""
        if len(clauses) == 1:
            return clauses[0]
        if len(clauses) == 2:
            return f"{clauses[0]} and {clauses[1]}"
        return ", ".join(clauses[:-1]) + f", and {clauses[-1]}"

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
            # Remove markdown heading markers but keep inline hashes (e.g. shebangs).
            line = re.sub(r"^#+\s*", "", line)
            line = re.sub(r"^(\d+\.\s+|[-*]\s+)", "", line)
            lines.append(line)
        return "\n".join(lines).strip()

    def _enforce_allowed_content(self, text: str, task: TerraformTask) -> str:
        if not text:
            raise ValueError("LLM produced empty instructions.")
        lowered = text.lower()
        for term in DISALLOWED_TERMS:
            pattern = _DISALLOWED_TERM_PATTERNS.get(term)
            if pattern and pattern.search(lowered):
                raise ValueError(f"LLM output contained disallowed term: {term}")
        provider_terms = self._provider_terms(task)
        if provider_terms and not any(self._contains_required_detail(text, term) for term in provider_terms):
            raise ValueError(
                f"LLM output missing required detail: {task.provider.lower()}"
            )
        # Packaging requirement: accept either keyword to reduce brittle failures.
        # The submission format is still a ZIP archive, but prompts may use either term.
        packaging_terms = ("zip", "archive")
        if not any(self._contains_required_detail(text, term) for term in packaging_terms):
            raise ValueError("LLM output missing required detail: zip or archive")
        for required in self._required_terms(task):
            if required in packaging_terms:
                continue
            if not self._contains_required_detail(text, required):
                raise ValueError(f"LLM output missing required detail: {required}")
        return text

    def _ensure_provider_reference(self, text: str, task: TerraformTask) -> str:
        provider_terms = self._provider_terms(task)
        lowered = text.lower()
        if provider_terms and not any(term in lowered for term in provider_terms):
            prefix = random.choice(
                [
                    f"In a {self._provider_label(task)} project, ",
                    f"Within {self._provider_label(task)}, ",
                    f"On {self._provider_label(task)}, ",
                    f"For a {self._provider_label(task)} sandbox, ",
                ]
            )
            text = f"{prefix}{text.lstrip()}"
        return text

    @staticmethod
    def _required_terms(task: TerraformTask) -> set[str]:
        required: set[str] = {"zip", "archive", "terraform.tfstate"}
        validator_sa = (task.validator_sa or "").strip().lower()
        metadata = getattr(task.spec, "metadata", None) or {}
        if validator_sa and isinstance(metadata, dict) and metadata.get("requires_validator_access"):
            required.add(validator_sa)
        for invariant in task.spec.invariants:
            match = getattr(invariant, "match", None) or {}
            if not isinstance(match, dict):
                continue
            for field, value in match.items():
                for term in TaskInstructionGenerator._iter_match_terms(str(field), value):
                    if not term:
                        continue
                    if len(term) > 80:
                        continue
                    if any(delim in term for delim in ("\n", "\r", "\t")):
                        continue
                    required.add(term)
        return required

    @staticmethod
    def _pinned_terms_for_llm(task: TerraformTask) -> set[str]:
        """
        Terms we explicitly ask the LLM to include.

        This is derived from `_required_terms()` but omits synonym tokens that can
        distract generation (e.g. asking for both `true` and `yes`).
        """
        required = TaskInstructionGenerator._required_terms(task)
        # Prefer requiring `zip` explicitly; validator accepts either zip|archive.
        pinned: set[str] = {"zip", "terraform.tfstate"}

        for term in required:
            if term in {"zip", "archive", "terraform.tfstate"}:
                continue
            # Avoid yes/no synonym tokens; `true`/`false` are sufficient.
            if term in {"yes", "no"}:
                continue
            # If both "two" and "2" exist, only ask for the digit form.
            if term.isalpha():
                value = TaskInstructionGenerator._small_int_value(term)
                if value is not None and str(value) in required:
                    continue
            pinned.add(term)

        return {TaskInstructionGenerator._llm_friendly_required_token(term) for term in pinned}

    @staticmethod
    def _llm_friendly_required_token(token: str) -> str:
        """
        Rewrite a required token into a more natural form for the LLM prompt.

        This does not affect invariant enforcement/validation (which is case-insensitive
        in prompt checks), but it helps keep the miner-facing prompt readable.
        """
        raw = (token or "").strip()
        if not raw:
            return raw
        if len(raw) < 3:
            return raw
        if "@" in raw or "." in raw or "/" in raw:
            return raw
        if _UPPER_ENUM_TOKEN_RE.fullmatch(raw) and any(ch.isalpha() for ch in raw):
            return raw.lower()
        return raw

    @staticmethod
    def _iter_match_terms(field: str, value: object) -> Iterable[str]:
        """
        Field-aware term extraction for invariant match dicts.

        For sentence-like fields (e.g., descriptions) we avoid requiring the
        entire sentence verbatim; instead we only require embedded identifiers.
        """
        if value is None:
            return

        field_lower = (field or "").strip().lower()
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return

            lowered = raw.lower()

            # IAM member strings can include a type prefix (e.g. "serviceAccount:foo").
            # Miners often mention only the principal identifier (e.g. "foo"), so require
            # the exact principal part while allowing the prefix to be omitted.
            if field_lower.endswith(".member") or field_lower.endswith("member"):
                if ":" in lowered and not lowered.startswith(("http://", "https://")):
                    _, principal = lowered.split(":", 1)
                    principal = principal.strip()
                    if principal:
                        yield principal
                        return

            is_description = field_lower.endswith("description") or field_lower.endswith(".description")
            # Only require "stable" identifier-like tokens. Filter out known static tokens and
            # terms without digits (e.g. "acore-token") that appear in many templates and are
            # not useful for differentiating tasks.
            identifier_terms = [
                term
                for term in _IDENTIFIER_TOKEN_RE.findall(lowered)
                if term not in _IGNORED_IDENTIFIER_TERMS and any(ch.isdigit() for ch in term)
            ]

            # If this string contains stable identifier tokens and reads like a phrase,
            # require only the identifier(s) rather than the entire sentence verbatim.
            # This avoids brittle failures for short description-like fields such as
            # "Allow SSH only for <token>" where the LLM may vary capitalization.
            if identifier_terms and any(ch.isspace() for ch in lowered):
                for term in identifier_terms:
                    yield term
                return

            # For description-like fields, require only stable identifier tokens.
            if is_description:
                for term in identifier_terms:
                    yield term
                return

            # If the string looks like a sentence, only require embedded identifiers (if any).
            word_count = len(lowered.split())
            if word_count >= 6:
                if identifier_terms:
                    for term in identifier_terms:
                        yield term
                return

            # Otherwise, keep the existing behavior (require the full literal).
            yield from TaskInstructionGenerator._iter_value_terms(value)
            return

        yield from TaskInstructionGenerator._iter_value_terms(value)

    @staticmethod
    def _iter_value_terms(value: object) -> Iterable[str]:
        """
        Yield human-mentionable leaf terms from an invariant value.

        The invariant match values can be scalars (str/int/bool) but also lists/dicts.
        Using `str(value)` for complex values creates brittle requirements and can
        cause false negatives even when the prompt is semantically correct.
        """
        if value is None:
            return
        if isinstance(value, str):
            term = value.strip().lower()
            # Do not require MIME-like values (e.g. "text/plain") to be echoed verbatim in
            # natural language prompts; miners can still infer the exact field value from
            # the validator JSON/task payload.
            if term and re.match(r"^[a-z0-9.+-]+/[a-z0-9.+-]+$", term) and not any(ch.isdigit() for ch in term):
                return
            if term:
                yield term
            return
        if isinstance(value, bool):
            # Keep the literal ("true"/"false") so it can be checked or auto-fixed, but
            # matching should also tolerate natural paraphrases (enabled/disabled).
            if value:
                yield "true"
                yield "yes"
            else:
                yield "false"
                yield "no"
            return
        if isinstance(value, (int, float)):
            term = str(value).strip().lower()
            if term:
                yield term
            if isinstance(value, int):
                word = TaskInstructionGenerator._small_int_word(value)
                if word:
                    yield word
            return
        if isinstance(value, dict):
            for nested in value.values():
                yield from TaskInstructionGenerator._iter_value_terms(nested)
            return
        if isinstance(value, (list, tuple, set)):
            for nested in value:
                yield from TaskInstructionGenerator._iter_value_terms(nested)
            return

        term = str(value).strip().lower()
        if term:
            yield term

    @staticmethod
    def _small_int_word(value: int) -> Optional[str]:
        # Minimal mapping; enough to avoid common "one/two/three" paraphrase failures.
        words = {
            0: "zero",
            1: "one",
            2: "two",
            3: "three",
            4: "four",
            5: "five",
            6: "six",
            7: "seven",
            8: "eight",
            9: "nine",
            10: "ten",
            11: "eleven",
            12: "twelve",
            13: "thirteen",
            14: "fourteen",
            15: "fifteen",
            16: "sixteen",
            17: "seventeen",
            18: "eighteen",
            19: "nineteen",
            20: "twenty",
        }
        return words.get(value)

    @staticmethod
    def _small_int_value(word: str) -> Optional[int]:
        key = (word or "").strip().lower()
        if not key:
            return None
        for value in range(0, 21):
            if TaskInstructionGenerator._small_int_word(value) == key:
                return value
        return None

    @staticmethod
    def _normalize_for_required_match(text: str) -> str:
        # Lowercase and drop non-alphanumerics so `us-central1` matches `us central1`, etc.
        lowered = text.lower()
        return re.sub(r"[^a-z0-9]+", "", lowered)

    @staticmethod
    def _contains_required_detail(text: str, required: str) -> bool:
        if not required:
            return True
        lowered = text.lower()
        required_lower = required.lower()
        if required_lower.isdigit():
            try:
                word = TaskInstructionGenerator._small_int_word(int(required_lower))
            except Exception:
                word = None
            if word and re.search(rf"\\b{re.escape(word)}\\b", lowered):
                return True
        else:
            value = TaskInstructionGenerator._small_int_value(required_lower)
            if value is not None and re.search(rf"\\b{value}\\b", lowered):
                return True
        if required_lower in ("true", "false", "yes", "no"):
            if required_lower in lowered:
                return True
            truthy = required_lower in ("true", "yes")
            falsy = required_lower in ("false", "no")
            # Allow yes/no synonyms to match explicit boolean literals.
            if truthy and re.search(r"\btrue\b", lowered):
                return True
            if falsy and re.search(r"\bfalse\b", lowered):
                return True
            if truthy and re.search(r"\b(enabled|enable|enabling|on)\b", lowered):
                return True
            if falsy and re.search(r"\b(disabled|disable|disabling|off)\b", lowered):
                return True
        if required_lower in lowered:
            return True
        # Fallback: tolerant matching across punctuation/quotes/whitespace differences.
        return (
            TaskInstructionGenerator._normalize_for_required_match(required_lower)
            in TaskInstructionGenerator._normalize_for_required_match(lowered)
        )

    @staticmethod
    def _provider_terms(task: TerraformTask) -> set[str]:
        provider_slug = task.provider.lower()
        synonyms = PROVIDER_SYNONYMS.get(provider_slug)
        if not synonyms:
            return {provider_slug}
        return {synonym.lower() for synonym in synonyms}

    @staticmethod
    def _provider_label(task: TerraformTask) -> str:
        """Human-friendly provider name for instructions."""
        provider_slug = task.provider.lower()
        label = {
            "aws": "AWS",
            "gcp": "Google Cloud",
            "azure": "Microsoft Azure",
        }.get(provider_slug)
        return label or provider_slug

    @staticmethod
    def _missing_detail_from_error(exc: Exception) -> Optional[str]:
        message = str(exc)
        marker = "missing required detail:"
        if marker not in message:
            return None
        fragment = message.split(marker, 1)[-1].strip()
        return fragment or None

    @staticmethod
    def _disallowed_term_from_error(exc: Exception) -> Optional[str]:
        message = str(exc)
        marker = "contained disallowed term:"
        if marker not in message:
            return None
        fragment = message.split(marker, 1)[-1].strip()
        return fragment or None

    @staticmethod
    def _missing_detail_origin(task: TerraformTask, missing_term: str) -> dict:
        """
        Best-effort mapping from a missing required term back to its source.

        This helps debug cases where the missing term is derived from a value in
        an invariant match dict or from the validator service account.
        """
        validator_sa = (getattr(task, "validator_sa", None) or "").strip()
        if validator_sa and missing_term == validator_sa.lower():
            return {"source": "validator_sa", "value": validator_sa}

        if missing_term.strip().lower() in {"zip or archive", "zip", "archive"}:
            return {"source": "packaging", "terms": ["zip", "archive"]}

        try:
            invariants = getattr(getattr(task, "spec", None), "invariants", None) or []
        except Exception:
            invariants = []

        for invariant in invariants:
            match = getattr(invariant, "match", None) or {}
            if not isinstance(match, dict):
                continue
            for field, value in match.items():
                for term in TaskInstructionGenerator._iter_match_terms(str(field), value):
                    if term == missing_term:
                        try:
                            json.dumps(value)
                            safe_value: object = value
                        except Exception:
                            safe_value = str(value)
                        return {
                            "source": "invariant",
                            "resource_type": getattr(invariant, "resource_type", None),
                            "field": str(field),
                            "value": safe_value,
                            "value_type": type(value).__name__,
                        }

        return {"source": "unknown"}
