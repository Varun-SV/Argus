# Argus ATES Test Execution Report

**Run:** `RUN-79de4a07a32442cbbb84aaafd3bfd4e5`
**Canonical status:** `failed`
**Evidence trust:** `bound_verified`
**Report trust:** `unverified_derived`

> Derived view only; canonical ATES evidence remains authoritative.

## Execution / source

    {
      "adapter_type": "cli",
      "argus_version": "0.0.0+golden",
      "configuration_commitment": {
        "canonicalization_profile": "ates-runtime-config-v1",
        "method": "hmac-sha256",
        "value": "hmac:8c4825a7f31db6d69b8359173154f79f0d31c51b964cb0ed9d1df046079812ed",
        "verification_ref": "protected://ates-runtime-identity-key"
      },
      "environment_type": "local",
      "evidence_profile": "ates-privacy-v1",
      "execution_kind": "scripted",
      "model": "MODEL-11cf071e75a00c3d903c1d7e",
      "model_provider": "ollama",
      "provider": "ollama",
      "run_id": "RUN-79de4a07a32442cbbb84aaafd3bfd4e5",
      "source": {
        "commitment": {
          "canonicalization_profile": "ates-runtime-scripted-source-v1",
          "method": "hmac-sha256",
          "value": "hmac:21bcf101794532d83d9cb2034e13a9368fef5cc5f987f048f5513d06649de6fa",
          "verification_ref": "protected://ates-runtime-identity-key"
        },
        "kind": "test_spec",
        "test_case_id": "TEST-38ad8c4bc8b8c85673808958"
      },
      "started_at": "2026-01-01T00:00:00.095000+00:00"
    }

## Outcome

    {
      "authorization": null,
      "correction_ids": [],
      "effective_status": "failed",
      "evidence_revision": 1,
      "finalization_id": "FINAL-d1cd6efadb5e48bdb79b3bd3df27d915",
      "finalized_at": "2026-01-01T00:00:00.114000+00:00",
      "revision": 1,
      "run_id": "RUN-79de4a07a32442cbbb84aaafd3bfd4e5",
      "status_policy_version": "ates-status-v1",
      "supersedes_finalization_id": null,
      "verification": null
    }

## Logical steps

    [
      {
        "instruction": {
          "disposition": "redacted",
          "protected_ref": null,
          "reason": "privacy.authored_text",
          "secret_refs": [],
          "value": "<redacted>"
        },
        "kind": "assert",
        "step_id": "STEP-89b552e153924534a63ebfb3deb35e75"
      }
    ]

## Attempt history

    [
      {
        "attempt": 1,
        "ended_at": "2026-01-01T00:00:00.107000+00:00",
        "retry_reason": null,
        "started_at": "2026-01-01T00:00:00.101000+00:00",
        "status": "failed",
        "step_attempt_id": "STEPATT-ba4b4f1005754333ac87a5f4b07da82c",
        "step_id": "STEP-89b552e153924534a63ebfb3deb35e75"
      }
    ]

## Assertions

    [
      {
        "actual": {
          "disposition": "suppressed",
          "protected_ref": null,
          "reason": "privacy.target_generated",
          "secret_refs": [],
          "value": null
        },
        "assertion_id": "ASSERT-e8be7f3059654b35b288d36dcb82aa3a",
        "expected": {
          "disposition": "redacted",
          "protected_ref": null,
          "reason": "privacy.assertion_value",
          "secret_refs": [],
          "value": "<redacted>"
        },
        "kind": "exit_code_is",
        "method": "deterministic.adapter_observation",
        "observation_id": "OBS-91938f3e465844dfad65b379a2a6977a",
        "required": true,
        "requirement": null,
        "result": "failed",
        "step_attempt_id": "STEPATT-ba4b4f1005754333ac87a5f4b07da82c",
        "step_id": "STEP-89b552e153924534a63ebfb3deb35e75"
      }
    ]

## Observations

    [
      {
        "capture_policy": "ates-privacy-v1",
        "captured_at": "2026-01-01T00:00:00.103000+00:00",
        "facts": {
          "dialog_count": {
            "disposition": "safe",
            "protected_ref": null,
            "reason": null,
            "secret_refs": [],
            "value": 0
          },
          "dialogs": {
            "disposition": "suppressed",
            "protected_ref": null,
            "reason": "privacy.target_generated",
            "secret_refs": [],
            "value": null
          },
          "element_count": {
            "disposition": "safe",
            "protected_ref": null,
            "reason": null,
            "secret_refs": [],
            "value": 0
          },
          "error": {
            "disposition": "safe",
            "protected_ref": null,
            "reason": null,
            "secret_refs": [],
            "value": null
          },
          "has_error": {
            "disposition": "safe",
            "protected_ref": null,
            "reason": null,
            "secret_refs": [],
            "value": false
          },
          "has_stderr": {
            "disposition": "safe",
            "protected_ref": null,
            "reason": null,
            "secret_refs": [],
            "value": true
          },
          "has_stdout": {
            "disposition": "safe",
            "protected_ref": null,
            "reason": null,
            "secret_refs": [],
            "value": false
          },
          "has_url": {
            "disposition": "safe",
            "protected_ref": null,
            "reason": null,
            "secret_refs": [],
            "value": false
          },
          "process_alive": {
            "disposition": "safe",
            "protected_ref": null,
            "reason": null,
            "secret_refs": [],
            "value": false
          },
          "screenshot_present": {
            "disposition": "safe",
            "protected_ref": null,
            "reason": null,
            "secret_refs": [],
            "value": false
          },
          "stderr": {
            "disposition": "suppressed",
            "protected_ref": null,
            "reason": "privacy.target_generated",
            "secret_refs": [],
            "value": null
          },
          "stdout": {
            "disposition": "safe",
            "protected_ref": null,
            "reason": null,
            "secret_refs": [],
            "value": null
          },
          "ui_tree": {
            "disposition": "suppressed",
            "protected_ref": null,
            "reason": "privacy.target_generated",
            "secret_refs": [],
            "value": null
          },
          "url": {
            "disposition": "safe",
            "protected_ref": null,
            "reason": null,
            "secret_refs": [],
            "value": null
          },
          "window_title": {
            "disposition": "suppressed",
            "protected_ref": null,
            "reason": "privacy.target_generated",
            "secret_refs": [],
            "value": null
          }
        },
        "observation_id": "OBS-91938f3e465844dfad65b379a2a6977a",
        "source": "cli",
        "step_attempt_id": "STEPATT-ba4b4f1005754333ac87a5f4b07da82c"
      }
    ]

## Artifacts (inert references only)

    [
      {
        "sequence": 6,
        "suppressed": {
          "artifact_id": "ART-5557b8114198485ab8692efa18d04f58",
          "capture_policy": "ates-artifact-v1",
          "context": "failure_screenshot",
          "kind": "screenshot",
          "reason": "artifact.screenshot_unavailable",
          "step_attempt_id": "STEPATT-ba4b4f1005754333ac87a5f4b07da82c"
        }
      }
    ]

## Findings

    []

## Failures / ambiguous outcomes

    [
      {
        "record": {
          "actual": {
            "disposition": "suppressed",
            "protected_ref": null,
            "reason": "privacy.target_generated",
            "secret_refs": [],
            "value": null
          },
          "assertion_id": "ASSERT-e8be7f3059654b35b288d36dcb82aa3a",
          "expected": {
            "disposition": "redacted",
            "protected_ref": null,
            "reason": "privacy.assertion_value",
            "secret_refs": [],
            "value": "<redacted>"
          },
          "kind": "exit_code_is",
          "method": "deterministic.adapter_observation",
          "observation_id": "OBS-91938f3e465844dfad65b379a2a6977a",
          "required": true,
          "requirement": null,
          "result": "failed",
          "step_attempt_id": "STEPATT-ba4b4f1005754333ac87a5f4b07da82c",
          "step_id": "STEP-89b552e153924534a63ebfb3deb35e75"
        },
        "sequence": 7,
        "type": "assertion"
      },
      {
        "record": {
          "attempt": 1,
          "ended_at": "2026-01-01T00:00:00.107000+00:00",
          "retry_reason": null,
          "started_at": "2026-01-01T00:00:00.101000+00:00",
          "status": "failed",
          "step_attempt_id": "STEPATT-ba4b4f1005754333ac87a5f4b07da82c",
          "step_id": "STEP-89b552e153924534a63ebfb3deb35e75"
        },
        "sequence": 8,
        "type": "step_attempt"
      },
      {
        "execution_result": "fail",
        "reason": "runtime.finalization_pending",
        "sequence": 11,
        "type": "run_incomplete"
      }
    ]

## Requirement traceability

    []

## Approvals

    {
      "records": [],
      "verified_effective_approval_count": 0
    }

## Audit

    {
      "local_chain_state": "locally_chain_verified",
      "records": [
        {
          "actor": "argus.finalizer",
          "audit_id": "AUDIT-487f57e07662480b9e838dd865cbc599",
          "dedupe_key": "finalization:FINAL-d1cd6efadb5e48bdb79b3bd3df27d915",
          "details": {
            "effective_status": "failed",
            "evidence_revision": 1,
            "finalization_id": "FINAL-d1cd6efadb5e48bdb79b3bd3df27d915",
            "manifest_digest": "sha256:7f1e92772139f06c0542499f7d47d842a8f71613ddc08334f6a93f2dd24b6a06",
            "revision": 1,
            "run_id": "RUN-79de4a07a32442cbbb84aaafd3bfd4e5"
          },
          "event_type": "finalization.bound",
          "ledger_version": "ates-audit-ledger-v1",
          "occurred_at": "2026-01-01T00:00:00.114000+00:00",
          "previous_record_digest": null
        }
      ],
      "trust_note": "The local chain is not an external tamper-evidence boundary."
    }

## Timeline

    [
      {
        "event_id": "EVT-db27ae171d1d4c0c87ba1ac8741c9774",
        "event_type": "RUN_STARTED",
        "occurred_at": "2026-01-01T00:00:00.096000+00:00",
        "sequence": 1
      },
      {
        "event_id": "EVT-5fdd518f4cf24749bb38f0f9b0d9fc0d",
        "event_type": "ENVIRONMENT_PREPARED",
        "occurred_at": "2026-01-01T00:00:00.097000+00:00",
        "sequence": 2
      },
      {
        "event_id": "EVT-f0eb05d505664ac397e78a1e585d908e",
        "event_type": "TARGET_LAUNCHED",
        "occurred_at": "2026-01-01T00:00:00.098000+00:00",
        "sequence": 3
      },
      {
        "event_id": "EVT-2c6b95a959d94473b016cacc02d37a94",
        "event_type": "STEP_ATTEMPT_STARTED",
        "occurred_at": "2026-01-01T00:00:00.102000+00:00",
        "sequence": 4
      },
      {
        "event_id": "EVT-9c56d3c3fc40426d9be8755594e82966",
        "event_type": "OBSERVATION_CAPTURED",
        "occurred_at": "2026-01-01T00:00:00.104000+00:00",
        "sequence": 5
      },
      {
        "event_id": "EVT-0e6fe7e105de4481991bb56b393d2116",
        "event_type": "ARTIFACT_SUPPRESSED",
        "occurred_at": "2026-01-01T00:00:00.105000+00:00",
        "sequence": 6
      },
      {
        "event_id": "EVT-4e254a8825154d569463d451c0f19373",
        "event_type": "ASSERTION_EVALUATED",
        "occurred_at": "2026-01-01T00:00:00.106000+00:00",
        "sequence": 7
      },
      {
        "event_id": "EVT-810894c3f1e44b419d3d483455523225",
        "event_type": "STEP_ATTEMPT_COMPLETED",
        "occurred_at": "2026-01-01T00:00:00.108000+00:00",
        "sequence": 8
      },
      {
        "event_id": "EVT-551e3f0238144fd8b5fd63f56d4c0ad5",
        "event_type": "TARGET_CLOSED",
        "occurred_at": "2026-01-01T00:00:00.110000+00:00",
        "sequence": 9
      },
      {
        "event_id": "EVT-ad30a10027e347dc9ce5e72df013399e",
        "event_type": "ENVIRONMENT_RELEASED",
        "occurred_at": "2026-01-01T00:00:00.112000+00:00",
        "sequence": 10
      },
      {
        "event_id": "EVT-ab0e2cbcb2dc4afebc6d2ace8129f7d9",
        "event_type": "RUN_MARKED_INCOMPLETE",
        "occurred_at": "2026-01-01T00:00:00.113000+00:00",
        "sequence": 11
      },
      {
        "event_id": "EVT-c82a81e017ce4528ba7a6a5482c805f3",
        "event_type": "RUN_COMPLETED",
        "occurred_at": "2026-01-01T00:00:00.114000+00:00",
        "sequence": 12
      }
    ]

## Integrity / source binding

    {
      "detached_ledger_snapshot": {
        "freshness_note": "These entries identify the detached-ledger state used to render this report. A member may be explicitly absent; present-member digests do not prove the files are still current. Call verify_report_bundle().",
        "members": [
          {
            "path": "approvals.jsonl",
            "sha256": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "size_bytes": 0,
            "state": "present"
          },
          {
            "path": "audit.jsonl",
            "sha256": "sha256:ad434ce28c69b3d870f338e869435f80a4a0658d9306ecf9f5870c4044b2335a",
            "size_bytes": 574,
            "state": "present"
          }
        ],
        "snapshot_version": "ates-detached-ledger-snapshot-v1"
      },
      "evidence_manifest_path": "manifests/manifest-0001.json",
      "evidence_manifest_sha256": "sha256:7f1e92772139f06c0542499f7d47d842a8f71613ddc08334f6a93f2dd24b6a06",
      "evidence_revision": 1,
      "finalization_id": "FINAL-d1cd6efadb5e48bdb79b3bd3df27d915",
      "run_id": "RUN-79de4a07a32442cbbb84aaafd3bfd4e5"
    }
