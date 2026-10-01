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


def _attest_installed_profile(definition: EnvironmentDefinition, health: dict, client) -> None:
    """Check OS-specific facts reported by the authenticated secure guest agent."""
    installation = definition.installation
    if installation.target_os is None:
        return
    if health.get("os_id") != installation.target_os:
        raise ProvisioningError("installed OS identity differs from environment definition")
    if installation.target_os == "windows-11":
        if (
            health.get("os_release") != installation.target_release
            or not isinstance(health.get("os_build"), int)
            or health["os_build"] < 22000
            or health.get("os_edition") != installation.edition
        ):
            raise ProvisioningError("installed Windows release or edition differs")
        if (
            health.get("target_user") != "argus-target"
            or health.get("target_user_present") is not True
            or health.get("target_user_non_admin") is not True
        ):
            raise ProvisioningError("Windows target-user policy is invalid")
        return
    if installation.target_os == "ubuntu":
        release = installation.target_release
        if release is None or health.get("os_release") != ".".join(release.split(".")[:2]):
            raise ProvisioningError("installed Ubuntu release differs")
        meta = {
            "desktop": "ubuntu-desktop", "server": "ubuntu-server",
        }.get(installation.target_flavor)
        if meta is None:
            raise ProvisioningError("installed Ubuntu flavor cannot be attested")
        required = tuple(dict.fromkeys((*installation.packages, meta)))
        installed = client.installed_packages(required)
        if not all(isinstance(installed.get(name), str) and installed[name] for name in required):
            raise ProvisioningError("installed Ubuntu flavor or requested packages are missing")
        if (
            health.get("target_user") != "argus"
            or health.get("target_user_present") is not True
            or health.get("target_user_non_admin") is not True
            or health.get("target_user_locked") is not True
        ):
            raise ProvisioningError("Ubuntu target-user policy is invalid")


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

    expected_runtime = definition.require_guest_runtime().runtime_identity

    def boot_once() -> tuple[str, str]:
        environment = SecureCapsuleExecutionEnvironment("cli", bound)
        failed = False
        capsule_id = ""
        machine_identity = ""
        try:
            environment.prepare()
            client = environment._client
            if client is None:
                raise ProvisioningError("baseline Capsule has no secure guest client")
            health = client.health()
            capsule_id = str(health.get("capsule_id") or "")
            machine_identity = str(health.get("machine_identity") or "")
            if (
                health.get("ok") is not True
                or health.get("service") != "argus-guest-agent"
                or health.get("secure") is not True
                or health.get("auth_session_id") != environment.session_id
                or capsule_id != environment._capsule_id
                or int(health.get("control_generation") or 0) != 1
                or health.get("runtime_identity") != expected_runtime
                or health.get("guest_os") != expected_os
                or health.get("architecture") != definition.machine.architecture
                or not machine_identity
            ):
                raise ProvisioningError(
                    "baseline guest OS, runtime, or generation identity is invalid"
                )
            _attest_installed_profile(definition, health, client)
        except Exception:
            failed = True
        finally:
            try:
                environment.close()
            except Exception:
                raise ProvisioningCleanupError(
                    "baseline Capsule teardown is uncertain"
                ) from None
            if environment._handle is not None:
                raise ProvisioningCleanupError(
                    "baseline Capsule teardown is uncertain"
                )
        if failed:
            raise ProvisioningError(
                "baseline Capsule boot or agent validation failed"
            )
        return capsule_id, machine_identity

    first_capsule, first_machine = boot_once()
    second_capsule, second_machine = boot_once()
    if first_capsule == second_capsule or first_machine == second_machine:
        raise ProvisioningError(
            "generalization validation did not produce fresh Capsule/OS identity"
        )
