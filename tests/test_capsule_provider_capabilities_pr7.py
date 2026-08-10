from __future__ import annotations

from argus.execution.secure_capsule import SecureCapsuleExecutionEnvironment


def test_secure_hyperv_provider_advertises_reference_capabilities():
    provider = SecureCapsuleExecutionEnvironment._make_secure_provider("auto", "win32")
    caps = provider.capabilities()

    assert provider.provider_name == "hyperv"
    assert caps.provider == "hyperv"
    assert caps.host_platforms == ("windows",)
    assert caps.guest_os == ("windows",)
    assert caps.secure_transport is True
    assert caps.network_isolation is True
    assert caps.explicit_transfers is True
    assert caps.failure_retention is True
    assert caps.egress_allowlist is True


def test_secure_libvirt_provider_advertises_linux_capabilities():
    provider = SecureCapsuleExecutionEnvironment._make_secure_provider("auto", "linux")
    caps = provider.capabilities()

    assert provider.provider_name == "libvirt"
    assert caps.provider == "libvirt"
    assert caps.host_platforms == ("linux",)
    assert caps.guest_os == ("linux",)
    assert caps.secure_transport is True
    assert caps.network_isolation is True
    assert caps.explicit_transfers is True
    assert caps.failure_retention is True
    assert caps.egress_allowlist is False
