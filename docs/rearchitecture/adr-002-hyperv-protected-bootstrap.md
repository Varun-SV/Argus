# ADR-002: Protected bootstrap delivery for Hyper-V Capsules

Status: **PROPOSED / OPEN DESIGN. Decision required before P6 starts.** Date opened: 2026-10-07.

Related: [re-architecture specification](specification.md) (§12 phase P6, §17, [Amendment B R-5](specification.md#18-amendment-b--reconciliation-decisions)), [stabilization specification CAP-07 and gate A04](../stabilization-spec.md#4-capsule-setup-and-preflight--arg-02), [ADR-001](tech-stack-decision.md).

Keywords MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

## 1. Problem

ISO-provisioned Capsules need secret-bearing bootstrap material in the guest: per-generation credentials and TLS trust for the guest agent. CAP-07 allows ISO-provisioned control only when that material reaches the guest in a way the non-admin target user cannot read. The libvirt provider has such a path. Hyper-V has none that Argus accepts. No document designs one yet, although CAP-07, gate A04 and the Linux-on-Hyper-V feature (specification §17) all depend on it.

Facts from the code at `8e7ebe5`:

- `ProviderCapabilities.protected_bootstrap_media` defaults to `False` (`argus/capsule/base.py`, line 51).
- Only the libvirt provider sets it to `True` (`argus/capsule/libvirt.py`, line 63).
- The Hyper-V provider does not advertise it. The guard in `argus/execution/secure_capsule.py` (line 286) therefore rejects ISO-provisioned control on Hyper-V: "ISO-provisioned control is disabled until a protected delivery path is implemented and verified".
- The current Hyper-V bootstrap medium is an NTFS VHDX on a fixed SCSI disk with an ACL that grants access only to SYSTEM and Administrators (`argus/capsule/windows_bootstrap.py`). Nothing verifies on the guest that the target user can read neither the secret files nor the raw medium. For that reason it is **not** accepted as protected.

## 2. Requirements

Any accepted design MUST meet all of these:

1. **CAP-07 access rule.** The non-admin target user cannot read the secret files, and cannot read the raw medium or the block device that carries it.
2. **Guest-side verification.** The guest proves the rule in requirement 1 by attempting the reads as the target user, and the result is recorded in the provisioning or runtime evidence. A host-side assertion alone does not count.
3. **Cleanup fencing.** A failed or uncertain detachment of the medium keeps the existing cleanup uncertainty fencing (CAP-05, CAP-07).
4. **No per-image secrets.** No secret is baked into a golden image; images stay reusable and immutable (PR #27).
5. **Per-generation credentials unchanged.** Fresh credentials and TLS material per Capsule generation, with bearer rotation, exactly as today.
6. **Both guest families.** The design works for Windows guests and for Linux guests on Hyper-V, because [specification §17](specification.md#17-first-feature-after-the-switch-linux-capsules-on-windows-hosts) reuses it.
7. **No early capability flag.** `protected_bootstrap_media` MUST NOT be set for Hyper-V before the guest-side verification of requirement 2 exists and passes natively. Setting the flag, mocking the baseline or bypassing the guard does not count as implementation (CAP-07).

## 3. Candidate options

These are candidates to evaluate, not a ranking. Options MAY be combined.

| Option | How it works | Strengths | Weaknesses and open points |
|---|---|---|---|
| A. Boot-service-only medium, detached before the target session | The medium is attached at boot. A boot-time service running as SYSTEM or root reads it, the host detaches it, and only then does the target session start. The guest then proves the medium and any copied secret files are not readable by the target user. | Closest to today's flow and to the libvirt path. Small change on the host. | Relies on ordering between detachment and target logon. Secrets copied inside the guest still need protection there. Detachment can fail, which triggers fencing (requirement 3). |
| B. One-time enrollment nonce | The medium carries only a short-lived, single-use nonce. The guest agent exchanges it over the control channel for its per-generation credentials, and the host invalidates it on first use. | A leaked medium is worth little once the nonce is used. Combines well with A. | Window between boot and enrollment. Replay and race handling must be designed. The exchange needs a trust anchor for the host. |
| C. vTPM-sealed material | Bootstrap secrets are sealed to the guest's virtual TPM, so only the measured boot path can unseal them. | Strong binding to the VM instance. | Needs a vTPM for every guest, including Linux guests on Hyper-V. Sealing policy and PCR choice per guest OS. More complex provisioning. |
| D. Guest-initiated enrollment over the isolated switch with a pinned host key | The guest agent starts with no secret, connects to the host over the internal switch, and verifies a pinned host public key from the image or the medium. The host then issues per-generation credentials. | No secret on any medium. | The pinned key must itself be delivered with integrity. Network isolation and the extended port ACLs must allow exactly this exchange. Needs an identity proof from the guest. |

## 4. Decision procedure

1. The design is written up from these options and reviewed by the operator.
2. The decision is recorded in this ADR, which changes status from PROPOSED to ACCEPTED, before phase P6 starts.
3. It is implemented and **natively accepted** on a real Hyper-V host, with Windows guests, as part of gate P6 (A04, CAP-07). Linux guests on Hyper-V are accepted with specification §17.5.
4. No capability flag is set before guest-side verification exists (requirement 7).

## 5. Status

**Decision required before P6 starts.** Until it is accepted, ISO-provisioned control on Hyper-V stays disabled, exactly as today.
