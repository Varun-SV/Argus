"""Boot a candidate image through the existing secure Capsule for baseline proof."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from argus.capsule.base import CapsuleSettings
from argus.execution.secure_capsule import SecureCapsuleExecutionEnvironment
from argus.provisioning.build import ProvisioningCleanupError
from argus.provisioning.capsule_bridge import capsule_settings_from_derived_image
from argus.provisioning.model import DerivedImageManifest, EnvironmentDefinition, ProvisioningError


def validate_secure_capsule_baseline(
    definition: EnvironmentDefinition,
    image: Path,
    *,
    provider: str,
    image_format: str,
    settings: CapsuleSettings,
) -> None:
    """Require the installed guest and secure agent to boot on a disposable child.

    The installer disk is never booted writable by this check. Capsule owns the
    overlay, network boundary, HTTPS control and teardown. A failed check must
    prevent publication; uncertain teardown retains the private build workspace.
    """
    expected_os = {"hyperv": "windows", "libvirt": "linux"}.get(provider)
    if expected_os is None:
        raise ProvisioningError("unsupported baseline Capsule provider")
    if (
        settings.guest_transport.lower() != "https"
        or settings.allow_insecure_http
        or settings.retain_on_failure
    ):
        raise ProvisioningError("baseline validation requires disposable HTTPS Capsules")

    digest = sha256()
    with image.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    provisional = DerivedImageManifest(
        environment_id=definition.environment_id,
        definition_sha256=definition.definition_sha256,
        source_sha256=definition.source.sha256,
        provider=provider,
        image_format=image_format,
        image_sha256=digest.hexdigest(),
        architecture=definition.machine.architecture,
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    bound = capsule_settings_from_derived_image(
        definition, provisional, image, settings=settings
    )
    if bound.guest_os not in {"auto", expected_os}:
        raise ProvisioningError("baseline guest OS contradicts provider contract")

    environment = SecureCapsuleExecutionEnvironment("cli", bound)
    problem: Exception | None = None
    try:
        environment.prepare()
        client = environment._client
        if client is None:
            raise ProvisioningError("baseline Capsule has no secure guest client")
        health = client.health()
        if (
            health.get("ok") is not True
            or health.get("service") != "argus-guest-agent"
            or health.get("secure") is not True
            or health.get("auth_session_id") != environment.session_id
            or health.get("guest_os") != expected_os
            or health.get("architecture") != definition.machine.architecture
        ):
            raise ProvisioningError("baseline guest OS or secure agent identity is invalid")
    except Exception as exc:
        problem = exc
    finally:
        try:
            environment.close()
        except Exception as exc:
            raise ProvisioningCleanupError("baseline Capsule teardown is uncertain") from exc
        if environment._handle is not None:
            raise ProvisioningCleanupError("baseline Capsule teardown is uncertain")
    if problem is not None:
        # Guest and hypervisor exceptions may contain operator-supplied input.
        raise ProvisioningError("baseline Capsule boot or agent validation failed") from problem
