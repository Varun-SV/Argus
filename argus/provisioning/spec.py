"""Strict YAML loader for Argus OS environment definitions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

from argus.provisioning.model import (
    EnvironmentDefinition,
    GuestRuntimeIdentity,
    InstallationMediaSource,
    InstallationSpec,
    MachineSpec,
    ProvisioningError,
)


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProvisioningError(f"{field} must be a mapping")
    return dict(value)


def _strict_mapping(
    value: Any,
    field: str,
    allowed: set[str],
    *,
    required: set[str] | None = None,
) -> dict[str, Any]:
    result = _mapping(value, field)
    unknown = sorted(set(result) - allowed)
    if unknown:
        raise ProvisioningError(
            f"unknown {field} field(s): " + ", ".join(unknown)
        )
    missing = sorted((required or set()) - set(result))
    if missing:
        raise ProvisioningError(
            f"missing {field} field(s): " + ", ".join(missing)
        )
    return result


def environment_definition_from_mapping(value: Mapping[str, Any]) -> EnvironmentDefinition:
    root = _strict_mapping(
        value,
        "environment",
        {"schema_version", "name", "source", "machine", "installation", "guest_runtime"},
        required={"name", "source"},
    )

    source = _strict_mapping(
        root["source"],
        "source",
        {"kind", "media_type", "path", "sha256", "architecture"},
        required={"path", "sha256"},
    )
    kind = str(source.pop("kind", "installation_media")).strip().lower()
    if kind != "installation_media":
        raise ProvisioningError("source.kind must be 'installation_media'")

    machine = _strict_mapping(
        root.get("machine", {}),
        "machine",
        {
            "architecture",
            "cpu_count",
            "memory_mb",
            "firmware",
            "secure_boot",
            "tpm_version",
            "disk_size_gib",
            "disk_bus",
            "network_mode",
        },
    )
    guest_runtime = None
    if "guest_runtime" in root:
        runtime = _strict_mapping(
            root["guest_runtime"],
            "guest_runtime",
            {
                "bundle_path",
                "runtime_bundle_sha256",
                "runtime_version",
                "target_os",
                "target_architecture",
                "bundle_format_version",
                "bootstrap_schema_version",
                "bootstrap_service_policy_version",
                "runtime_installation_policy_version",
            },
            required={
                "bundle_path",
                "runtime_bundle_sha256",
                "runtime_version",
                "target_os",
            },
        )
        guest_runtime = GuestRuntimeIdentity(**runtime)

    installation = _strict_mapping(
        root.get("installation", {}),
        "installation",
        {
            "unattended",
            "edition",
            "locale",
            "timezone",
            "packages",
            "update_policy",
            "credential_ref",
            "target_os",
            "target_release",
            "target_flavor",
            "apt_mirror",
        },
    )

    return EnvironmentDefinition(
        schema_version=root.get("schema_version", "argus-environment-v1"),
        name=root["name"],
        source=InstallationMediaSource(**source),
        machine=MachineSpec(**machine),
        installation=InstallationSpec(**installation),
        guest_runtime=guest_runtime,
    )


def load_environment_definition(path: str | Path) -> EnvironmentDefinition:
    definition_path = Path(path).expanduser()
    try:
        raw = yaml.safe_load(definition_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ProvisioningError(
            f"cannot load environment definition {definition_path}: {exc}"
        ) from exc
    if not isinstance(raw, Mapping):
        raise ProvisioningError("environment definition root must be a mapping")
    return environment_definition_from_mapping(raw)
