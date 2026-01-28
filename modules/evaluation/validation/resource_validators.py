"""Resource-specific validators for GCP resources."""
import base64
from typing import Any, Callable, Dict

from modules.models import Invariant
from modules.evaluation.validation.models import InvariantValidation
from modules.evaluation.validation.state_parser import TerraformStateParser


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _matches_case_insensitive(actual: Any, expected: Any) -> bool:
    actual_str = _as_str(actual)
    expected_str = _as_str(expected)
    if actual_str is None or expected_str is None:
        return False
    return actual_str.lower() == expected_str.lower()


def _matches_name(actual: Any, expected: Any) -> bool:
    """Accept exact, case-insensitive, or prefix match for names when expected is shorter."""
    actual_str = _as_str(actual) or ""
    expected_str = _as_str(expected) or ""
    if not actual_str and not expected_str:
        return True
    if actual_str == expected_str:
        return True
    if actual_str.lower() == expected_str.lower():
        return True
    if expected_str and len(expected_str) < len(actual_str) and actual_str.startswith(expected_str):
        return True
    return False


def _apply_comparison_rule(actual: Any, expected: Any, rule: str) -> bool:
    """
    Apply the specified comparison rule to validate a field.

    Args:
        actual: Actual value from state file
        expected: Expected value from invariant
        rule: Comparison rule (exact_match, starts_with, ends_with)

    Returns:
        True if the comparison passes, False otherwise
    """
    actual_str = _as_str(actual)
    expected_str = _as_str(expected)

    if not actual_str or not expected_str:
        return actual == expected

    if rule == "starts_with":
        return actual_str.startswith(expected_str) and len(actual_str) > len(expected_str)
    elif rule == "ends_with":
        return actual_str.endswith(expected_str) and len(actual_str) > len(expected_str)
    else:  # exact_match (default)
        if actual_str == expected_str:
            return True
        if _matches_case_insensitive(actual_str, expected_str):
            return True
        if _matches_ref(actual_str, expected_str):
            return True
        return False


def _comparison_rule_for(invariant: Invariant, path: str) -> str | None:
    if not getattr(invariant, "comparison_rule", None):
        return None
    return invariant.comparison_rule.get(path)


def _match_name_with_rule(actual: Any, expected: Any, rule: str | None) -> bool:
    if rule:
        return _apply_comparison_rule(actual, expected, rule)
    return _matches_name(actual, expected)


def _name_mismatch_error(path: str, expected: Any, actual: Any, rule: str | None) -> str:
    if rule == "starts_with":
        return f"{path}: expected to start with '{expected}', got '{actual}'"
    if rule == "ends_with":
        return f"{path}: expected to end with '{expected}', got '{actual}'"
    return f"{path}: expected name '{expected}', got '{actual}'"


def _matches_ref(actual: Any, expected: Any) -> bool:
    """
    Match a Terraform reference-like attribute.

    Terraform state often stores a full self_link while task invariants store the
    friendly name (e.g. "net-123" vs "projects/.../global/networks/net-123").
    """
    actual_str = _as_str(actual)
    expected_str = _as_str(expected)
    if not actual_str or not expected_str:
        return actual == expected
    if "{project_id}" in expected_str:
        prefix, suffix = expected_str.split("{project_id}", 1)
        if actual_str.startswith(prefix) and actual_str.endswith(suffix):
            return True
    if actual_str == expected_str:
        return True
    actual_last = actual_str.rstrip("/").split("/")[-1]
    expected_last = expected_str.rstrip("/").split("/")[-1]
    if actual_last and expected_last and actual_last.lower() == expected_last.lower():
        return True
    if actual_str.endswith(f"/{expected_str}") or f"/{expected_str}" in actual_str:
        return True
    if expected_str.endswith(f"/{actual_str}") or f"/{actual_str}" in expected_str:
        return True
    return False


def _rstrip_newlines(value: Any) -> Any:
    if isinstance(value, str):
        return value.rstrip()
    return value


def _normalize_startup_script(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]
    return "\n".join(lines).rstrip()


def _default_validate(
    invariant: Invariant,
    resource: Dict[str, Any],
    parser: TerraformStateParser,
) -> InvariantValidation:
    """
    Default validation: supports exact, starts_with, and ends_with matching for fields.

    Args:
        invariant: The invariant to validate (with optional comparison_rule)
        resource: Resource from Terraform state
        parser: State parser for accessing attributes

    Returns:
        InvariantValidation with pass/fail status
    """
    result = InvariantValidation(
        resource_type=invariant.resource_type,
        invariant_match=invariant.match,
        passed=True,
        actual_values={},
    )

    for path, expected_value in invariant.match.items():
        actual_value = parser.get_resource_attribute(resource, path)
        result.actual_values[path] = actual_value

        # Get comparison rule for this field (default to exact_match)
        rule = invariant.comparison_rule.get(path, "exact_match")

        # Apply the appropriate comparison
        matches = _apply_comparison_rule(actual_value, expected_value, rule)

        if not matches:
            result.passed = False
            if rule == "starts_with":
                result.errors.append(
                    f"{path}: expected to start with '{expected_value}', got '{actual_value}'"
                )
            elif rule == "ends_with":
                result.errors.append(
                    f"{path}: expected to end with '{expected_value}', got '{actual_value}'"
                )
            else:
                result.errors.append(
                    f"{path}: expected '{expected_value}', got '{actual_value}'"
                )

    return result


def _validate_dns_record_set(
    invariant: Invariant,
    resource: Dict[str, Any],
    parser: TerraformStateParser,
) -> InvariantValidation:
    """
    DNS record sets contain list attributes (rrdatas) where ordering should not matter.
    """
    result = InvariantValidation(
        resource_type=invariant.resource_type,
        invariant_match=invariant.match,
        passed=True,
        actual_values={},
    )

    for path, expected_value in invariant.match.items():
        actual_value = parser.get_resource_attribute(resource, path)
        result.actual_values[path] = actual_value

        if path.endswith(".rrdatas") or path.endswith("values.rrdatas"):
            expected_list = list(expected_value or []) if isinstance(expected_value, list) else [expected_value]
            actual_list = list(actual_value or []) if isinstance(actual_value, list) else [actual_value]
            if sorted(map(str, expected_list)) != sorted(map(str, actual_list)):
                result.passed = False
                result.errors.append(f"{path}: expected rrdatas {expected_list}, got {actual_list}")
            continue

        if actual_value != expected_value:
            result.passed = False
            result.errors.append(f"{path}: expected '{expected_value}', got '{actual_value}'")

    return result


def _validate_compute_network(
    invariant: Invariant,
    resource: Dict[str, Any],
    parser: TerraformStateParser,
) -> InvariantValidation:
    result = InvariantValidation(
        resource_type=invariant.resource_type,
        invariant_match=invariant.match,
        passed=True,
        actual_values={},
    )

    for path, expected_value in invariant.match.items():
        actual_value = parser.get_resource_attribute(resource, path)
        result.actual_values[path] = actual_value

        if path.endswith(".name") or path.endswith("values.name"):
            rule = _comparison_rule_for(invariant, path)
            if not _match_name_with_rule(actual_value, expected_value, rule):
                result.passed = False
                result.errors.append(_name_mismatch_error(path, expected_value, actual_value, rule))
            continue

        if actual_value != expected_value:
            result.passed = False
            result.errors.append(f"{path}: expected '{expected_value}', got '{actual_value}'")

    return result


def _validate_compute_subnetwork(
    invariant: Invariant,
    resource: Dict[str, Any],
    parser: TerraformStateParser,
) -> InvariantValidation:
    result = InvariantValidation(
        resource_type=invariant.resource_type,
        invariant_match=invariant.match,
        passed=True,
        actual_values={},
    )

    for path, expected_value in invariant.match.items():
        actual_value = parser.get_resource_attribute(resource, path)
        result.actual_values[path] = actual_value

        if path.endswith(".network") or path.endswith("values.network"):
            if not _matches_ref(actual_value, expected_value):
                result.passed = False
                result.errors.append(
                    f"{path}: expected network '{expected_value}', got '{actual_value}'"
                )
            continue
        if path.endswith(".name") or path.endswith("values.name"):
            rule = _comparison_rule_for(invariant, path)
            if not _match_name_with_rule(actual_value, expected_value, rule):
                result.passed = False
                result.errors.append(_name_mismatch_error(path, expected_value, actual_value, rule))
            continue
        if path.endswith(".region") or path.endswith("values.region"):
            if not _matches_case_insensitive(actual_value, expected_value):
                result.passed = False
                result.errors.append(f"{path}: expected region '{expected_value}', got '{actual_value}'")
            continue

        if actual_value != expected_value:
            result.passed = False
            result.errors.append(f"{path}: expected '{expected_value}', got '{actual_value}'")

    return result


def _validate_compute_instance(
    invariant: Invariant,
    resource: Dict[str, Any],
    parser: TerraformStateParser,
) -> InvariantValidation:
    result = InvariantValidation(
        resource_type=invariant.resource_type,
        invariant_match=invariant.match,
        passed=True,
        actual_values={},
    )

    for path, expected_value in invariant.match.items():
        actual_value = parser.get_resource_attribute(resource, path)
        result.actual_values[path] = actual_value

        if path.endswith(".machine_type") or path.endswith("values.machine_type"):
            if not (_matches_ref(actual_value, expected_value) or _matches_case_insensitive(actual_value, expected_value)):
                result.passed = False
                result.errors.append(
                    f"{path}: expected machine type '{expected_value}', got '{actual_value}'"
                )
            continue

        if path.endswith(".subnetwork") or path.endswith("values.network_interface.0.subnetwork"):
            candidate = actual_value
            if candidate is None and not path.endswith("values.network_interface.0.subnetwork"):
                candidate = parser.get_resource_attribute(resource, "values.network_interface.0.subnetwork")
            if not _matches_ref(candidate, expected_value):
                result.passed = False
                result.errors.append(
                    f"{path}: expected subnetwork '{expected_value}', got '{candidate}'"
                )
            continue
        if path.endswith(".network") or path.endswith("values.network_interface.0.network"):
            candidate = actual_value
            if candidate is None and not path.endswith("values.network_interface.0.network"):
                candidate = parser.get_resource_attribute(resource, "values.network_interface.0.network")
            if not _matches_ref(candidate, expected_value):
                result.passed = False
                result.errors.append(
                    f"{path}: expected network '{expected_value}', got '{candidate}'"
                )
            continue

        if path.endswith(".name") or path.endswith("values.name"):
            rule = _comparison_rule_for(invariant, path)
            if not _match_name_with_rule(actual_value, expected_value, rule):
                result.passed = False
                result.errors.append(_name_mismatch_error(path, expected_value, actual_value, rule))
            continue

        if "metadata_startup_script" in path:
            if _normalize_startup_script(actual_value) != _normalize_startup_script(expected_value):
                result.passed = False
                result.errors.append(
                    f"{path}: expected startup script mismatch"
                )
            continue

        if actual_value != expected_value:
            result.passed = False
            result.errors.append(f"{path}: expected '{expected_value}', got '{actual_value}'")

    return result


def _validate_compute_firewall(
    invariant: Invariant,
    resource: Dict[str, Any],
    parser: TerraformStateParser,
) -> InvariantValidation:
    result = InvariantValidation(
        resource_type=invariant.resource_type,
        invariant_match=invariant.match,
        passed=True,
        actual_values={},
    )

    expected_protocol = invariant.match.get("values.allow.0.protocol")
    expected_port = invariant.match.get("values.allow.0.ports.0")

    # Validate scalar fields.
    for path, expected_value in invariant.match.items():
        if path.startswith("values.allow."):
            continue
        actual_value = parser.get_resource_attribute(resource, path)
        result.actual_values[path] = actual_value

        if path.endswith(".network") or path.endswith("values.network"):
            if not _matches_ref(actual_value, expected_value):
                result.passed = False
                result.errors.append(
                    f"{path}: expected network '{expected_value}', got '{actual_value}'"
                )
            continue

        if path.endswith(".direction") or path.endswith("values.direction"):
            if not _matches_case_insensitive(actual_value, expected_value):
                result.passed = False
                result.errors.append(f"{path}: expected '{expected_value}', got '{actual_value}'")
            continue

        if path.endswith(".name") or path.endswith("values.name"):
            rule = _comparison_rule_for(invariant, path)
            if not _match_name_with_rule(actual_value, expected_value, rule):
                result.passed = False
                result.errors.append(_name_mismatch_error(path, expected_value, actual_value, rule))
            continue

        if actual_value != expected_value:
            result.passed = False
            result.errors.append(f"{path}: expected '{expected_value}', got '{actual_value}'")

    # Validate allow posture (match any allow block).
    if expected_protocol is not None:
        allow_blocks = resource.get("attributes", {}).get("allow") or []
        result.actual_values["values.allow"] = allow_blocks
        matched = False
        for block in allow_blocks:
            proto = (block or {}).get("protocol")
            ports = (block or {}).get("ports") or []
            if proto != expected_protocol:
                continue
            if expected_port is None:
                matched = True
                break
            if str(expected_port) in {str(p) for p in ports}:
                matched = True
                break
        if not matched:
            result.passed = False
            expected_desc = (
                f"{expected_protocol}/{expected_port}" if expected_port is not None else str(expected_protocol)
            )
            result.errors.append(f"values.allow: expected allow {expected_desc}, got {allow_blocks}")

    return result


def _validate_pubsub_subscription(
    invariant: Invariant,
    resource: Dict[str, Any],
    parser: TerraformStateParser,
) -> InvariantValidation:
    result = InvariantValidation(
        resource_type=invariant.resource_type,
        invariant_match=invariant.match,
        passed=True,
        actual_values={},
    )

    for path, expected_value in invariant.match.items():
        actual_value = parser.get_resource_attribute(resource, path)
        result.actual_values[path] = actual_value

        if path.endswith(".topic") or path.endswith("values.topic"):
            actual_str = _as_str(actual_value)
            expected_str = _as_str(expected_value)
            if actual_str and expected_str:
                if actual_str == expected_str or actual_str.endswith(f"/topics/{expected_str}") or actual_str.endswith(f"/{expected_str}"):
                    continue
            if actual_value != expected_value:
                result.passed = False
                result.errors.append(f"{path}: expected topic '{expected_value}', got '{actual_value}'")
            continue

        if path.endswith(".ttl") or path.endswith("values.ttl"):
            actual_ttl = parser.get_resource_attribute(resource, "values.expiration_policy.ttl")
            if actual_ttl is None:
                actual_ttl = parser.get_resource_attribute(resource, "values.expiration_policy.0.ttl")
            if actual_ttl is None:
                actual_ttl = actual_value
            if actual_ttl != expected_value:
                result.passed = False
                result.errors.append(f"{path}: expected ttl '{expected_value}', got '{actual_ttl}'")
            continue

        if actual_value != expected_value:
            result.passed = False
            result.errors.append(f"{path}: expected '{expected_value}', got '{actual_value}'")

    return result


def _validate_secret_manager_secret_version(
    invariant: Invariant,
    resource: Dict[str, Any],
    parser: TerraformStateParser,
) -> InvariantValidation:
    """
    Secret Manager secret version values can vary in state:
    - `secret` may be stored as a full resource name/self_link.
    - `secret_data` may appear as plain text or base64-encoded.
    """
    result = InvariantValidation(
        resource_type=invariant.resource_type,
        invariant_match=invariant.match,
        passed=True,
        actual_values={},
    )

    for path, expected_value in invariant.match.items():
        actual_value = parser.get_resource_attribute(resource, path)
        result.actual_values[path] = actual_value

        if path.endswith(".secret") or path.endswith("values.secret"):
            if not _matches_ref(actual_value, expected_value):
                result.passed = False
                result.errors.append(
                    f"{path}: expected secret '{expected_value}', got '{actual_value}'"
                )
            continue

        if path.endswith(".secret_data") or path.endswith("values.secret_data"):
            expected = _as_str(expected_value) or ""
            actual = _as_str(actual_value) or ""
            if actual == expected:
                continue
            # Accept base64 encoding of the expected plaintext.
            try:
                decoded = base64.b64decode(actual.encode("utf-8"), validate=True).decode(
                    "utf-8", errors="strict"
                )
                if decoded == expected:
                    continue
            except Exception:
                pass
            result.passed = False
            result.errors.append(f"{path}: expected secret_data (plaintext or b64) mismatch")
            continue

        if actual_value != expected_value:
            result.passed = False
            result.errors.append(f"{path}: expected '{expected_value}', got '{actual_value}'")

    return result


def _validate_secret_manager_secret_iam_member(
    invariant: Invariant,
    resource: Dict[str, Any],
    parser: TerraformStateParser,
) -> InvariantValidation:
    result = InvariantValidation(
        resource_type=invariant.resource_type,
        invariant_match=invariant.match,
        passed=True,
        actual_values={},
    )

    for path, expected_value in invariant.match.items():
        actual_value = parser.get_resource_attribute(resource, path)
        result.actual_values[path] = actual_value

        if path.endswith(".secret_id") or path.endswith("values.secret_id"):
            if not _matches_ref(actual_value, expected_value):
                result.passed = False
                result.errors.append(f"{path}: expected secret_id '{expected_value}', got '{actual_value}'")
            continue

        if path.endswith(".member") or path.endswith("values.member"):
            actual_str = (_as_str(actual_value) or "").lower()
            expected_str = (_as_str(expected_value) or "").lower()
            if expected_str and expected_str in actual_str:
                continue
            result.passed = False
            result.errors.append(f"{path}: expected member containing '{expected_value}', got '{actual_value}'")
            continue

        if actual_value != expected_value:
            result.passed = False
            result.errors.append(f"{path}: expected '{expected_value}', got '{actual_value}'")

    return result


def _validate_service_account_iam_member(
    invariant: Invariant,
    resource: Dict[str, Any],
    parser: TerraformStateParser,
) -> InvariantValidation:
    """
    `service_account_id` often expands into a fully-qualified resource name in state.
    """
    result = InvariantValidation(
        resource_type=invariant.resource_type,
        invariant_match=invariant.match,
        passed=True,
        actual_values={},
    )

    for path, expected_value in invariant.match.items():
        actual_value = parser.get_resource_attribute(resource, path)
        result.actual_values[path] = actual_value

        if path.endswith(".service_account_id") or path.endswith("values.service_account_id"):
            actual_str = (_as_str(actual_value) or "").lower()
            expected_str = (_as_str(expected_value) or "").lower()
            # Accept full name/self_link forms as long as the expected identifier appears.
            if expected_str and expected_str in actual_str:
                continue
            if not _matches_ref(actual_value, expected_value):
                result.passed = False
                result.errors.append(
                    f"{path}: expected service_account_id containing '{expected_value}', got '{actual_value}'"
                )
            continue

        if path.endswith(".member") or path.endswith("values.member"):
            actual_str = (_as_str(actual_value) or "").lower()
            expected_str = (_as_str(expected_value) or "").lower()
            if expected_str and expected_str in actual_str:
                continue
            result.passed = False
            result.errors.append(f"{path}: expected member containing '{expected_value}', got '{actual_value}'")
            continue

        if actual_value != expected_value:
            result.passed = False
            result.errors.append(f"{path}: expected '{expected_value}', got '{actual_value}'")

    return result


def _validate_storage_bucket_object(
    invariant: Invariant,
    resource: Dict[str, Any],
    parser: TerraformStateParser,
) -> InvariantValidation:
    """
    Bucket object content may be stored as plaintext, omitted, or represented via hashes.
    """
    import hashlib

    result = InvariantValidation(
        resource_type=invariant.resource_type,
        invariant_match=invariant.match,
        passed=True,
        actual_values={},
    )

    expected_content = invariant.match.get("values.content")

    for path, expected_value in invariant.match.items():
        actual_value = parser.get_resource_attribute(resource, path)
        result.actual_values[path] = actual_value

        if path.endswith(".content") or path.endswith("values.content"):
            expected = _as_str(expected_value) or ""
            actual = _as_str(actual_value)
            if actual is not None and actual == expected:
                continue

            # If state doesn't store content, accept md5hash/crc32c when available.
            attrs = resource.get("attributes", {}) if isinstance(resource, dict) else {}
            md5hash = _as_str((attrs or {}).get("md5hash"))
            if md5hash:
                expected_md5_b64 = base64.b64encode(hashlib.md5(expected.encode("utf-8")).digest()).decode("utf-8")
                if md5hash == expected_md5_b64:
                    continue

            result.passed = False
            result.errors.append(f"{path}: expected content (or matching hash) mismatch")
            continue

        if path.endswith(".bucket") or path.endswith("values.bucket"):
            if not _matches_ref(actual_value, expected_value):
                result.passed = False
                result.errors.append(f"{path}: expected '{expected_value}', got '{actual_value}'")
            continue

        if actual_value != expected_value:
            result.passed = False
            result.errors.append(f"{path}: expected '{expected_value}', got '{actual_value}'")

    # If the invariant didn't request content explicitly, nothing extra to do.
    _ = expected_content
    return result


def _validate_storage_bucket(
    invariant: Invariant,
    resource: Dict[str, Any],
    parser: TerraformStateParser,
) -> InvariantValidation:
    result = InvariantValidation(
        resource_type=invariant.resource_type,
        invariant_match=invariant.match,
        passed=True,
        actual_values={},
    )

    for path, expected_value in invariant.match.items():
        actual_value = parser.get_resource_attribute(resource, path)
        result.actual_values[path] = actual_value

        if path.endswith(".name") or path.endswith("values.name"):
            rule = invariant.comparison_rule.get(path, "exact_match")
            if not _apply_comparison_rule(actual_value, expected_value, rule):
                result.passed = False
                result.errors.append(f"{path}: expected name '{expected_value}', got '{actual_value}'")
            continue

        if path.endswith(".storage_class") or path.endswith("values.storage_class"):
            if not _matches_case_insensitive(actual_value, expected_value):
                result.passed = False
                result.errors.append(f"{path}: expected '{expected_value}', got '{actual_value}'")
            continue

        if path.endswith(".location") or path.endswith("values.location"):
            # Allow case-insensitive match and tolerate region self_link fragments
            actual_str = _as_str(actual_value) or ""
            expected_str = _as_str(expected_value) or ""
            if not _matches_case_insensitive(actual_str, expected_str):
                result.passed = False
                result.errors.append(f"{path}: expected '{expected_value}', got '{actual_value}'")
            continue

        if actual_value != expected_value:
            result.passed = False
            result.errors.append(f"{path}: expected '{expected_value}', got '{actual_value}'")

    return result


def _validate_dns_managed_zone(
    invariant: Invariant,
    resource: Dict[str, Any],
    parser: TerraformStateParser,
) -> InvariantValidation:
    result = InvariantValidation(
        resource_type=invariant.resource_type,
        invariant_match=invariant.match,
        passed=True,
        actual_values={},
    )

    for path, expected_value in invariant.match.items():
        actual_value = parser.get_resource_attribute(resource, path)
        result.actual_values[path] = actual_value

        if path.endswith(".name") or path.endswith("values.name"):
            rule = _comparison_rule_for(invariant, path)
            if not _match_name_with_rule(actual_value, expected_value, rule):
                result.passed = False
                result.errors.append(_name_mismatch_error(path, expected_value, actual_value, rule))
            continue

        if path.endswith(".dns_name") or path.endswith("values.dns_name"):
            actual_str = (_as_str(actual_value) or "").lower()
            expected_str = (_as_str(expected_value) or "").lower()
            if actual_str.endswith("."):
                actual_str = actual_str[:-1]
            if expected_str.endswith("."):
                expected_str = expected_str[:-1]
            if actual_str != expected_str:
                result.passed = False
                result.errors.append(f"{path}: expected dns_name '{expected_value}', got '{actual_value}'")
            continue

        if path.endswith(".visibility") or path.endswith("values.visibility"):
            if not _matches_case_insensitive(actual_value, expected_value):
                result.passed = False
                result.errors.append(f"{path}: expected visibility '{expected_value}', got '{actual_value}'")
            continue

        if actual_value != expected_value:
            result.passed = False
            result.errors.append(f"{path}: expected '{expected_value}', got '{actual_value}'")

    return result


def _validate_iam_member_prefix(
    invariant: Invariant,
    resource: Dict[str, Any],
    parser: TerraformStateParser,
) -> InvariantValidation:
    """
    IAM member strings commonly expand into full emails in state.

    Example:
      expected: serviceAccount:sa-12345678
      actual:   serviceAccount:sa-12345678@my-project.iam.gserviceaccount.com
    """
    result = InvariantValidation(
        resource_type=invariant.resource_type,
        invariant_match=invariant.match,
        passed=True,
        actual_values={},
    )

    for path, expected_value in invariant.match.items():
        actual_value = parser.get_resource_attribute(resource, path)
        result.actual_values[path] = actual_value

        if path.endswith(".member") or path.endswith("values.member"):
            actual_str = (_as_str(actual_value) or "").lower()
            expected_str = (_as_str(expected_value) or "").lower()
            if expected_str and expected_str in actual_str:
                continue
            result.passed = False
            result.errors.append(f"{path}: expected member containing '{expected_value}', got '{actual_value}'")
            continue

        if actual_value != expected_value:
            result.passed = False
            result.errors.append(f"{path}: expected '{expected_value}', got '{actual_value}'")

    return result


def _validate_cloud_scheduler_job(
    invariant: Invariant,
    resource: Dict[str, Any],
    parser: TerraformStateParser,
) -> InvariantValidation:
    """
    Cloud Scheduler jobs can expand Pub/Sub topic names into full resource paths.
    """
    result = InvariantValidation(
        resource_type=invariant.resource_type,
        invariant_match=invariant.match,
        passed=True,
        actual_values={},
    )

    for path, expected_value in invariant.match.items():
        actual_value = parser.get_resource_attribute(resource, path)
        result.actual_values[path] = actual_value

        if path.endswith(".pubsub_target.0.topic_name") or path.endswith("values.pubsub_target.0.topic_name"):
            if _matches_ref(actual_value, expected_value):
                continue
            result.passed = False
            result.errors.append(f"{path}: expected topic_name '{expected_value}', got '{actual_value}'")
            continue

        if actual_value != expected_value:
            result.passed = False
            result.errors.append(f"{path}: expected '{expected_value}', got '{actual_value}'")

    return result


def _validate_project_iam_custom_role(
    invariant: Invariant,
    resource: Dict[str, Any],
    parser: TerraformStateParser,
) -> InvariantValidation:
    """
    Custom role permissions can behave like an unordered collection in state.

    We validate that the required permission appears in the permissions list rather
    than relying on a stable index.
    """
    result = InvariantValidation(
        resource_type=invariant.resource_type,
        invariant_match=invariant.match,
        passed=True,
        actual_values={},
    )

    attrs = resource.get("attributes", {}) if isinstance(resource, dict) else {}
    permissions = (attrs or {}).get("permissions") or []

    for path, expected_value in invariant.match.items():
        if path.endswith(".permissions.0") or path.endswith("values.permissions.0"):
            result.actual_values[path] = permissions
            expected_str = _as_str(expected_value)
            if expected_str and any(_as_str(p) == expected_str for p in (permissions or [])):
                continue
            result.passed = False
            result.errors.append(f"{path}: expected permissions containing '{expected_value}', got '{permissions}'")
            continue

        actual_value = parser.get_resource_attribute(resource, path)
        result.actual_values[path] = actual_value
        if actual_value != expected_value:
            result.passed = False
            result.errors.append(f"{path}: expected '{expected_value}', got '{actual_value}'")

    return result


# Validator function type
ValidatorFunc = Callable[[Invariant, Dict[str, Any], TerraformStateParser], InvariantValidation]

# Registry of resource-specific validators
# Add custom validators here by resource type
_VALIDATORS: Dict[str, ValidatorFunc] = {
    "google_compute_network": _validate_compute_network,
    "google_compute_subnetwork": _validate_compute_subnetwork,
    "google_compute_firewall": _validate_compute_firewall,
    "google_compute_instance": _validate_compute_instance,
    "google_dns_managed_zone": _validate_dns_managed_zone,
    "google_dns_record_set": _validate_dns_record_set,
    "google_cloud_scheduler_job": _validate_cloud_scheduler_job,
    "google_pubsub_subscription": _validate_pubsub_subscription,
    "google_secret_manager_secret_version": _validate_secret_manager_secret_version,
    "google_secret_manager_secret_iam_member": _validate_secret_manager_secret_iam_member,
    "google_service_account_iam_member": _validate_service_account_iam_member,
    "google_storage_bucket": _validate_storage_bucket,
    "google_storage_bucket_object": _validate_storage_bucket_object,
    "google_project_iam_member": _validate_iam_member_prefix,
    "google_storage_bucket_iam_member": _validate_iam_member_prefix,
    "google_project_iam_custom_role": _validate_project_iam_custom_role,
}


def get_validator(resource_type: str) -> ValidatorFunc:
    """
    Get validator function for a resource type.

    Args:
        resource_type: GCP resource type (e.g., 'google_compute_instance')

    Returns:
        Validator function (defaults to exact string matching)

    Example - Add custom validator:
        >>> def _validate_compute_instance(invariant, resource, parser):
        ...     # Custom validation logic
        ...     return InvariantValidation(...)
        >>> _VALIDATORS["google_compute_instance"] = _validate_compute_instance
    """
    return _VALIDATORS.get(resource_type, _default_validate)
