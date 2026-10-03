"""Ubuntu Subiquity autoinstall answers and short-lived NoCloud seed media.

The caller must verify the operator's ISO digest before invoking this module.
The release/flavor labels are operator assertions, not an ISO inspection result.
Only a Linux/libvirt installer with one writable target disk is supported.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import base64
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from typing import Callable, Iterator, Sequence
from urllib.parse import urlsplit

import yaml

from argus.provisioning.model import EnvironmentDefinition, ProvisioningError
from argus.provisioning.build import ProvisioningCleanupError
from argus.provisioning.runtime_bundle import GuestRuntimeBundleManifest


_RELEASE_RE = re.compile(r"^(\d{2})\.(04|10)(?:\.(\d+))?$")
_HOSTNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,127}$")
_LOCALE_RE = re.compile(r"^[a-z]{2,3}(?:[-_][A-Z]{2})?(?:\.UTF-8)?$")
_TIMEZONE_RE = re.compile(r"^[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)+$")
_SHA512_CRYPT_RE = re.compile(
    r"^\$6\$(?:rounds=[1-9][0-9]{0,8}\$)?[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]{86}$"
)


def _systemd_argument(value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ProvisioningError("guest runtime entrypoint contains control characters")
    # The executable argument expands specifiers, but does not substitute
    # environment variables. Doubling '$' would change the executable pathname.
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped.replace("%", "%%") + '"'


def _ubuntu_locale(locale: str) -> str:
    if not _LOCALE_RE.fullmatch(locale):
        raise ProvisioningError("Ubuntu autoinstall requires a supported locale identifier")
    stem = locale.removesuffix(".UTF-8").replace("-", "_")
    return stem + ".UTF-8"


def _ubuntu_timezone(timezone: str) -> str:
    if timezone == "UTC":
        return "Etc/UTC"
    if not _TIMEZONE_RE.fullmatch(timezone):
        raise ProvisioningError("Ubuntu autoinstall requires a valid IANA timezone")
    return timezone


@dataclass(frozen=True)
class UbuntuAutoinstallProfile:
    """A Subiquity profile for a pinned Ubuntu ISO and a resolved credential.

    ``password_hash`` must come from the caller's approved ``secret://``
    resolver. It is deliberately absent from the environment definition and
    from this object's representation. It must never be logged or published.
    """

    definition: EnvironmentDefinition = field(repr=False)
    password_hash: str = field(repr=False)
    username: str = "argus"
    runtime_manifest: GuestRuntimeBundleManifest | None = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        installation = self.definition.installation
        if installation.target_os != "ubuntu":
            raise ProvisioningError("Ubuntu autoinstall requires target_os=ubuntu")
        flavor = installation.target_flavor
        release_label = installation.target_release
        if not isinstance(release_label, str):
            raise ProvisioningError("Ubuntu autoinstall requires target_release")
        match = _RELEASE_RE.fullmatch(release_label)
        if match is None:
            raise ProvisioningError("Ubuntu release must be a numbered .04 or .10 release")
        release = (int(match.group(1)), int(match.group(2)))
        if flavor not in {"server", "desktop"}:
            raise ProvisioningError("Ubuntu flavor must be server or desktop")
        minimum = (20, 4) if flavor == "server" else (23, 4)
        if release < minimum:
            raise ProvisioningError("Ubuntu ISO predates supported Subiquity autoinstall")
        if self.definition.source.media_type != "iso":
            raise ProvisioningError("Ubuntu autoinstall requires an ISO source")
        if self.definition.machine.architecture != "x86_64":
            raise ProvisioningError("Ubuntu autoinstall currently supports x86_64 only")
        if self.definition.machine.network_mode != "host_only":
            raise ProvisioningError("Ubuntu autoinstall requires host_only networking")
        if not installation.unattended:
            raise ProvisioningError("Ubuntu autoinstall requires unattended=true")
        if self.runtime_manifest is None and installation.credential_ref is None:
            raise ProvisioningError("Ubuntu autoinstall requires an approved credential_ref")
        if installation.edition is not None:
            raise ProvisioningError("Ubuntu autoinstall cannot guarantee edition selection")
        if installation.update_policy != "latest":
            raise ProvisioningError(
                "Ubuntu autoinstall currently supports update_policy=latest only"
            )
        mirror = installation.apt_mirror
        if mirror is None:
            raise ProvisioningError("Ubuntu autoinstall requires a pinned host-local apt mirror")
        try:
            parsed = urlsplit(mirror)
            valid_mirror = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment
                and all(part not in {".", ".."} for part in parsed.path.split("/"))
                and parsed.port != 0
                and not any(ord(char) < 33 for char in mirror)
            )
        except ValueError:
            valid_mirror = False
        if not valid_mirror:
            raise ProvisioningError("Ubuntu apt mirror URI is invalid or contains credentials")
        if not _USERNAME_RE.fullmatch(self.username):
            raise ProvisioningError("Ubuntu autoinstall username is invalid")
        if self.runtime_manifest is None and not _SHA512_CRYPT_RE.fullmatch(self.password_hash):
            raise ProvisioningError("Ubuntu autoinstall requires a SHA-512 crypt password hash")
        for package in installation.packages:
            if not _PACKAGE_RE.fullmatch(package):
                raise ProvisioningError("Ubuntu autoinstall package name is unsupported")
        _ubuntu_locale(installation.locale)
        _ubuntu_timezone(installation.timezone)
        if self.runtime_manifest is not None:
            identity = self.definition.require_guest_runtime()
            self.runtime_manifest.validate_identity(identity)
            if identity.target_os != "ubuntu":
                raise ProvisioningError(
                    "Ubuntu build requires an Ubuntu guest runtime bundle"
                )

    @property
    def hostname(self) -> str:
        hostname = "argus-" + self.definition.definition_sha256[:12]
        assert _HOSTNAME_RE.fullmatch(hostname)
        return hostname

    @property
    def kernel_cmdline(self) -> str:
        """Direct kernel boot argument; avoids Subiquity's disk wipe prompt."""
        return "autoinstall"

    @property
    def source_id(self) -> str:
        """Pin the OS payload; validate it against the staged ISO before boot."""
        return "ubuntu-desktop" if self.definition.installation.target_flavor == "desktop" else "ubuntu-server"

    def runtime_late_commands(self) -> list[str]:
        """Install the pinned runtime and generalize clone-sensitive state."""
        if self.runtime_manifest is None:
            return []
        identity = self.definition.require_guest_runtime()
        manifest = self.runtime_manifest
        manifest.validate_identity(identity)
        entrypoint = "/opt/argus/runtime/" + manifest.entrypoint
        service = (
            "[Unit]\n"
            "Description=Argus Capsule Bootstrap\n"
            "After=local-fs.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            "ExecStart=" + _systemd_argument(entrypoint)
            + " --bootstrap-service "
            + "--runtime-identity-file /etc/argus/runtime-identity.json "
            + "--control-state-file /var/lib/argus/control-state.json\n"
            # Bootstrap token/TLS key are one-attempt material. If the agent
            # exits after establishment, local restart must not try to reuse
            # the same generation; the host must establish a new generation.
            "Restart=no\n"
            "NoNewPrivileges=true\n\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        )
        service_b64 = base64.b64encode(service.encode("utf-8")).decode("ascii")
        bundle = "/mnt/argus-build/runtime-bundle.zip"
        target_bundle = "/target/tmp/argus-runtime.zip"
        target_entry = "/target" + entrypoint
        commands = [
            "mkdir -p /mnt/argus-build",
            "mount -o ro /dev/disk/by-label/ARGUS_BUILD /mnt/argus-build",
            (
                "printf '%s  %s\\n' "
                + shlex.quote(identity.runtime_bundle_sha256)
                + " "
                + shlex.quote(bundle)
                + " | sha256sum -c -"
            ),
            "mkdir -p /target/opt/argus/runtime /target/etc/argus "
            "/target/var/lib/argus",
            "cp " + shlex.quote(bundle) + " " + shlex.quote(target_bundle),
            "cp /mnt/argus-build/build-payload.json "
            "/target/etc/argus/runtime-identity.json",
            "chmod 0644 /target/etc/argus/runtime-identity.json",
            "chmod 0700 /target/var/lib/argus",
            (
                "curtin in-target --target=/target -- python3 -m zipfile -e "
                "/tmp/argus-runtime.zip /opt/argus/runtime"
            ),
            "chmod 0755 " + shlex.quote(target_entry),
            (
                "printf '%s' "
                + shlex.quote(service_b64)
                + " | base64 -d > "
                "/target/etc/systemd/system/argus-bootstrap.service"
            ),
            (
                "curtin in-target --target=/target -- "
                "systemctl enable argus-bootstrap.service"
            ),
            (
                "curtin in-target --target=/target -- "
                "usermod --password '!' " + shlex.quote(self.username)
            ),
            (
                "curtin in-target --target=/target -- /bin/sh -c "
                + shlex.quote(
                    "gpasswd -d " + self.username
                    + " sudo >/dev/null 2>&1 || true"
                )
            ),
            # Drop device groups and prevent the desktop broker from remounting
            # the root-only optical media for a persistent target application.
            "curtin in-target --target=/target -- usermod -G users " + shlex.quote(self.username),
            # logind otherwise grants active desktop users raw optical access
            # via 70-uaccess.rules, independently of groups and polkit. Remove
            # that tag before 73-seat-late runs its ACL builtin. Protect both
            # the optical block node and its SCSI command passthrough node.
            "mkdir -p /target/etc/udev/rules.d",
            "printf '%s' " + shlex.quote(base64.b64encode((
                'SUBSYSTEM=="block", ENV{ID_CDROM}=="1", TAG-="uaccess", '
                'OWNER:="root", GROUP:="root", MODE:="0600"\n'
                'SUBSYSTEM=="scsi_generic", SUBSYSTEMS=="scsi", ATTRS{type}=="4|5", '
                'TAG-="uaccess", OWNER:="root", GROUP:="root", MODE:="0600"\n'
            ).encode()).decode())
            + " | base64 -d > /target/etc/udev/rules.d/72-argus-optical.rules",
            "mkdir -p /target/etc/polkit-1/rules.d",
            "printf '%s' " + shlex.quote(base64.b64encode((
                "polkit.addRule(function(action, subject) {\n"
                "  if (subject.user == '" + self.username + "' &&\n"
                "      action.id.indexOf('org.freedesktop.udisks2.') === 0) {\n"
                "    return polkit.Result.NO;\n"
                "  }\n"
                "});\n"
            ).encode()).decode()) + " | base64 -d > /target/etc/polkit-1/rules.d/10-argus-media.rules",
            "rm -rf /target/var/lib/cloud/instance "
            "/target/var/lib/cloud/instances/* "
            "/target/var/lib/cloud/seed/nocloud*",
            "rm -f /target/etc/machine-id /target/var/lib/dbus/machine-id",
            ": > /target/etc/machine-id",
            "rm -f " + shlex.quote(target_bundle),
            "umount /mnt/argus-build",
        ]
        if self.definition.installation.target_flavor == "desktop":
            # Ubuntu Desktop requires a real X11 target session. The account's
            # password remains locked; GDM owns console login, not Argus auth.
            desktop_policy = (
                "[daemon]\nWaylandEnable=false\nAutomaticLoginEnable=true\n"
                "AutomaticLogin=" + self.username + "\n"
            )
            encoded = base64.b64encode(desktop_policy.encode()).decode("ascii")
            commands.insert(-1, "printf '%s' " + shlex.quote(encoded)
                            + " | base64 -d > /target/etc/gdm3/custom.conf")
        return commands

    def autoinstall_config(self) -> dict[str, object]:
        """Return a fresh mapping suitable for the NoCloud user-data file."""
        installation = self.definition.installation
        config: dict[str, object] = {
            "version": 1,
            "identity": {
                "hostname": self.hostname,
                "username": self.username,
                # Production images have no password credential to scrub from
                # Subiquity's repeatable answer file or installer diagnostics.
                # Compatibility answer-only profiles retain the old input;
                # publishable builds always supply a runtime manifest.
                "password": "!" if self.runtime_manifest is not None else self.password_hash,
            },
            "locale": _ubuntu_locale(installation.locale),
            "timezone": _ubuntu_timezone(installation.timezone),
            "source": {"id": self.source_id},
            "storage": {"layout": {"name": "direct"}},
            "apt": {
                "geoip": False,
                "mirror-selection": {"primary": [{"uri": installation.apt_mirror}]},
                "fallback": "abort",
            },
            "refresh-installer": {"update": False},
            "updates": "all",
            "shutdown": "poweroff",
        }
        if installation.packages:
            config["packages"] = list(installation.packages)
        late_commands = self.runtime_late_commands()
        if late_commands:
            config["late-commands"] = late_commands
        return config

    def user_data(self) -> str:
        """Render cloud-config; the result contains a sensitive password hash."""
        return "#cloud-config\n" + yaml.safe_dump(
            {"autoinstall": self.autoinstall_config()},
            sort_keys=False,
            allow_unicode=False,
        )

    def meta_data(self) -> str:
        return (
            "instance-id: iid-" + self.definition.definition_sha256[:32] + "\n"
            "local-hostname: " + self.hostname + "\n"
        )


def _run_cloud_localds(argv: Sequence[str], timeout: float) -> None:
    try:
        result = subprocess.run(
            list(argv), capture_output=True, text=True, check=False, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProvisioningError("NoCloud seed ISO creation failed or timed out") from exc
    if result.returncode:
        # Tool output could include installer answers. Never include it in errors.
        raise ProvisioningError("NoCloud seed ISO creation failed")


def _run_xorriso(argv: Sequence[str], timeout: float) -> None:
    try:
        result = subprocess.run(
            list(argv), capture_output=True, text=True, check=False, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProvisioningError("Ubuntu source manifest extraction failed or timed out") from exc
    if result.returncode:
        raise ProvisioningError("Ubuntu source manifest extraction failed")


def validate_ubuntu_source_id(
    staged_iso: Path,
    profile: UbuntuAutoinstallProfile,
    *,
    runner: Callable[[Sequence[str], float], None] | None = None,
    timeout: float = 30,
) -> None:
    """Ensure the pinned install source exists in verified, staged ISO media.

    ``xorriso`` reads only ``casper/install-sources.yaml``. Media without this
    manifest fail closed; no fallback to an unspecified default install source.
    The caller must separately verify the ISO digest before this check.
    """
    staged_iso = Path(staged_iso)
    try:
        iso_info = staged_iso.lstat()
    except OSError as exc:
        raise ProvisioningError("staged Ubuntu ISO is unavailable") from exc
    if not stat.S_ISREG(iso_info.st_mode) or iso_info.st_size == 0:
        raise ProvisioningError("staged Ubuntu ISO must be a nonempty regular file")
    if timeout <= 0:
        raise ProvisioningError("Ubuntu source manifest timeout must be positive")
    with tempfile.TemporaryDirectory(prefix="argus-ubuntu-sources-") as directory:
        manifest = Path(directory) / "install-sources.yaml"
        command = (
            "xorriso", "-osirrox", "on", "-indev", str(staged_iso),
            "-extract", "/casper/install-sources.yaml", str(manifest),
        )
        if runner is None:
            _run_xorriso(command, timeout)
        else:
            runner(command, timeout)
        try:
            info = manifest.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                raise ProvisioningError("Ubuntu source manifest is invalid")
            sources = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ProvisioningError("Ubuntu source manifest is invalid") from exc
        if not isinstance(sources, list):
            raise ProvisioningError("Ubuntu source manifest is invalid")
        matching = [source for source in sources if isinstance(source, dict)
                    and source.get("id") == profile.source_id]
        if len(matching) != 1:
            raise ProvisioningError("requested Ubuntu install source is unavailable")
        variant = matching[0].get("variant")
        if variant is not None and variant != profile.definition.installation.target_flavor:
            raise ProvisioningError("requested Ubuntu install source has wrong flavor")


@contextmanager
def temporary_nocloud_seed_iso(
    profile: UbuntuAutoinstallProfile,
    *,
    workspace: Path,
    qemu_gid: int | None = None,
    runner: Callable[[Sequence[str], float], None] | None = None,
    timeout: float = 60,
) -> Iterator[Path]:
    """Build a CIDATA ISO, expose it only during installation, then remove it.

    The workspace must be a private installer directory. When QEMU runs under a
    service account, pass its shared group ID so it can traverse the temporary
    directory and read the ISO. The sensitive input files are removed before
    group access is granted. The caller must keep the context open until the
    installer VM is destroyed, including cleanup on failure.
    """
    if not isinstance(workspace, Path):
        workspace = Path(workspace)
    if not workspace.is_dir() or workspace.is_symlink():
        raise ProvisioningError("NoCloud workspace must be an existing directory")
    if qemu_gid is not None and (
        isinstance(qemu_gid, bool) or not isinstance(qemu_gid, int) or qemu_gid < 0
    ):
        raise ProvisioningError("NoCloud QEMU group ID is invalid")
    if timeout <= 0:
        raise ProvisioningError("NoCloud seed timeout must be positive")
    if os.name != "posix" and runner is None:
        raise ProvisioningError("NoCloud seed ISO creation requires a Linux host")

    private = Path(tempfile.mkdtemp(prefix="argus-nocloud-", dir=workspace))
    os.chmod(private, 0o700)
    preserve_for_recovery = False
    try:
        user_data = private / "user-data"
        meta_data = private / "meta-data"
        seed = private / "seed.iso"
        user_data.write_text(profile.user_data(), encoding="utf-8")
        meta_data.write_text(profile.meta_data(), encoding="utf-8")
        os.chmod(user_data, 0o600)
        os.chmod(meta_data, 0o600)
        command = (
            "cloud-localds", "-f", "iso", str(seed), str(user_data), str(meta_data)
        )
        if runner is None:
            _run_cloud_localds(command, timeout)
        else:
            runner(command, timeout)
        info = seed.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0 or info.st_nlink != 1:
            raise ProvisioningError("NoCloud seed ISO is missing or invalid")
        user_data.unlink()
        meta_data.unlink()
        if set(private.iterdir()) != {seed}:
            raise ProvisioningError("NoCloud seed workspace contains unexpected files")
        if qemu_gid is not None:
            os.chown(seed, -1, qemu_gid)
            os.chown(private, -1, qemu_gid)
            os.chmod(seed, 0o640)
            os.chmod(private, 0o750)
        else:
            os.chmod(seed, 0o600)
        yield seed
    except ProvisioningCleanupError:
        # The VM may still hold this ISO open. Retain it with the private build
        # workspace so an operator can inspect and remove the orphan safely.
        preserve_for_recovery = True
        raise
    finally:
        if not preserve_for_recovery:
            shutil.rmtree(private)
