# Argus ATES Test Execution Report

**Run:** `RUN-34765ecacaf14128acb51334a3c81eb9`
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
        "value": "hmac:78fed05fb9482cccbdc7845b387a9445966663703d697da412a4fcd7c29f9cf5",
        "verification_ref": "protected://ates-runtime-identity-key"
      },
      "environment_type": "local",
      "evidence_profile": "ates-privacy-v1",
      "execution_kind": "scripted",
      "model": "MODEL-d3a3d37eb920df9670d4daae",
      "model_provider": "ollama",
      "provider": "ollama",
      "run_id": "RUN-34765ecacaf14128acb51334a3c81eb9",
      "source": {
        "commitment": {
          "canonicalization_profile": "ates-runtime-scripted-source-v1",
          "method": "hmac-sha256",
          "value": "hmac:497381f4e761be13c3551c536300684a30a4cc14856f1bec21b475115605ac09",
          "verification_ref": "protected://ates-runtime-identity-key"
        },
        "kind": "test_spec",
        "test_case_id": "TEST-29f21681f93f7f40a20e9fc0"
      },
      "started_at": "2026-01-01T00:00:00.050000+00:00"
    }

## Outcome

    {
      "authorization": null,
      "correction_ids": [],
      "effective_status": "failed",
      "evidence_revision": 1,
      "finalization_id": "FINAL-be3df88f06474ee592669ab21347ef85",
      "finalized_at": "2026-01-01T00:00:00.079000+00:00",
      "revision": 1,
      "run_id": "RUN-34765ecacaf14128acb51334a3c81eb9",
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
        "step_id": "STEP-adcb6188c35349798eca76d09f5685f2"
      },
      {
        "instruction": {
          "disposition": "redacted",
          "protected_ref": null,
          "reason": "privacy.authored_text",
          "secret_refs": [],
          "value": "<redacted>"
        },
        "kind": "assert",
        "step_id": "STEP-a5f327d64a3047b2802676ab131d49af"
      }
    ]

## Attempt history

    [
      {
        "attempt": 1,
        "ended_at": "2026-01-01T00:00:00.061000+00:00",
        "retry_reason": null,
        "started_at": "2026-01-01T00:00:00.056000+00:00",
        "status": "passed",
        "step_attempt_id": "STEPATT-41fa344df55840169f099e3e8f7e46fc",
        "step_id": "STEP-adcb6188c35349798eca76d09f5685f2"
      },
      {
        "attempt": 1,
        "ended_at": "2026-01-01T00:00:00.072000+00:00",
        "retry_reason": null,
        "started_at": "2026-01-01T00:00:00.066000+00:00",
        "status": "failed",
        "step_attempt_id": "STEPATT-1d1d9a663ff54967825114726797b996",
        "step_id": "STEP-a5f327d64a3047b2802676ab131d49af"
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
        "assertion_id": "ASSERT-00b4209c87174fb79dcee897a54c226c",
        "expected": {
          "disposition": "redacted",
          "protected_ref": null,
          "reason": "privacy.assertion_value",
          "secret_refs": [],
          "value": "<redacted>"
        },
        "kind": "stderr_contains",
        "method": "deterministic.adapter_observation",
        "observation_id": "OBS-12e5e142433b40b18f9baffd5a2b8de4",
        "required": true,
        "requirement": null,
        "result": "passed",
        "step_attempt_id": "STEPATT-41fa344df55840169f099e3e8f7e46fc",
        "step_id": "STEP-adcb6188c35349798eca76d09f5685f2"
      },
      {
        "actual": {
          "disposition": "suppressed",
          "protected_ref": null,
          "reason": "privacy.target_generated",
          "secret_refs": [],
          "value": null
        },
        "assertion_id": "ASSERT-607651fb194d441aae04c0221a030c35",
        "expected": {
          "disposition": "redacted",
          "protected_ref": null,
          "reason": "privacy.assertion_value",
          "secret_refs": [],
          "value": "<redacted>"
        },
        "kind": "exit_code_is",
        "method": "deterministic.adapter_observation",
        "observation_id": "OBS-a5439390cdb34a24b0b49e30a06e97c8",
        "required": true,
        "requirement": null,
        "result": "failed",
        "step_attempt_id": "STEPATT-1d1d9a663ff54967825114726797b996",
        "step_id": "STEP-a5f327d64a3047b2802676ab131d49af"
      }
    ]

## Observations

    [
      {
        "capture_policy": "ates-privacy-v1",
        "captured_at": "2026-01-01T00:00:00.058000+00:00",
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
            "value": true
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
            "value": true
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
            "disposition": "suppressed",
            "protected_ref": null,
            "reason": "privacy.target_generated",
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
        "observation_id": "OBS-12e5e142433b40b18f9baffd5a2b8de4",
        "source": "cli",
        "step_attempt_id": "STEPATT-41fa344df55840169f099e3e8f7e46fc"
      },
      {
        "capture_policy": "ates-privacy-v1",
        "captured_at": "2026-01-01T00:00:00.068000+00:00",
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
            "value": true
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
            "value": true
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
            "disposition": "suppressed",
            "protected_ref": null,
            "reason": "privacy.target_generated",
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
        "observation_id": "OBS-a5439390cdb34a24b0b49e30a06e97c8",
        "source": "cli",
        "step_attempt_id": "STEPATT-1d1d9a663ff54967825114726797b996"
      }
    ]

## Artifacts (inert references only)

    [
      {
        "sequence": 10,
        "suppressed": {
          "artifact_id": "ART-03637c903b0e44c0a5a8b54dee4dbdbe",
          "capture_policy": "ates-artifact-v1",
          "context": "failure_screenshot",
          "kind": "screenshot",
          "reason": "artifact.screenshot_unavailable",
          "step_attempt_id": "STEPATT-1d1d9a663ff54967825114726797b996"
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
          "assertion_id": "ASSERT-607651fb194d441aae04c0221a030c35",
          "expected": {
            "disposition": "redacted",
            "protected_ref": null,
            "reason": "privacy.assertion_value",
            "secret_refs": [],
            "value": "<redacted>"
          },
          "kind": "exit_code_is",
          "method": "deterministic.adapter_observation",
          "observation_id": "OBS-a5439390cdb34a24b0b49e30a06e97c8",
          "required": true,
          "requirement": null,
          "result": "failed",
          "step_attempt_id": "STEPATT-1d1d9a663ff54967825114726797b996",
          "step_id": "STEP-a5f327d64a3047b2802676ab131d49af"
        },
        "sequence": 11,
        "type": "assertion"
      },
      {
        "record": {
          "attempt": 1,
          "ended_at": "2026-01-01T00:00:00.072000+00:00",
          "retry_reason": null,
          "started_at": "2026-01-01T00:00:00.066000+00:00",
          "status": "failed",
          "step_attempt_id": "STEPATT-1d1d9a663ff54967825114726797b996",
          "step_id": "STEP-a5f327d64a3047b2802676ab131d49af"
        },
        "sequence": 12,
        "type": "step_attempt"
      },
      {
        "execution_result": "fail",
        "reason": "runtime.finalization_pending",
        "sequence": 15,
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
          "audit_id": "AUDIT-7de236489ddf46d59daeb1065545ebf8",
          "dedupe_key": "finalization:FINAL-be3df88f06474ee592669ab21347ef85",
          "details": {
            "effective_status": "failed",
            "evidence_revision": 1,
            "finalization_id": "FINAL-be3df88f06474ee592669ab21347ef85",
            "manifest_digest": "sha256:71f0422ceb306507e88d025e2529c745c236286f9aab864bca33d9a5c7f5ea3d",
            "revision": 1,
            "run_id": "RUN-34765ecacaf14128acb51334a3c81eb9"
          },
          "event_type": "finalization.bound",
          "ledger_version": "ates-audit-ledger-v1",
          "occurred_at": "2026-01-01T00:00:00.079000+00:00",
          "previous_record_digest": null
        }
      ],
      "trust_note": "The local chain is not an external tamper-evidence boundary."
    }

## Timeline

    [
      {
        "event_id": "EVT-0428467656124be9848528c3056575d6",
        "event_type": "RUN_STARTED",
        "occurred_at": "2026-01-01T00:00:00.051000+00:00",
        "sequence": 1
      },
      {
        "event_id": "EVT-138897f1b8fd442bbb673bf4bceba0f5",
        "event_type": "ENVIRONMENT_PREPARED",
        "occurred_at": "2026-01-01T00:00:00.052000+00:00",
        "sequence": 2
      },
      {
        "event_id": "EVT-545c5396726b4b40904091ee19269422",
        "event_type": "TARGET_LAUNCHED",
        "occurred_at": "2026-01-01T00:00:00.053000+00:00",
        "sequence": 3
      },
      {
        "event_id": "EVT-ea7d3abd2fc446639d4d41148fa7b49a",
        "event_type": "STEP_ATTEMPT_STARTED",
        "occurred_at": "2026-01-01T00:00:00.057000+00:00",
        "sequence": 4
      },
      {
        "event_id": "EVT-abdb5bfe922d458c9b8a09d740607581",
        "event_type": "OBSERVATION_CAPTURED",
        "occurred_at": "2026-01-01T00:00:00.059000+00:00",
        "sequence": 5
      },
      {
        "event_id": "EVT-296a754274c24c9197157fc2eadd110d",
        "event_type": "ASSERTION_EVALUATED",
        "occurred_at": "2026-01-01T00:00:00.060000+00:00",
        "sequence": 6
      },
      {
        "event_id": "EVT-ca43884139a74353b0f4ab63ea87874f",
        "event_type": "STEP_ATTEMPT_COMPLETED",
        "occurred_at": "2026-01-01T00:00:00.062000+00:00",
        "sequence": 7
      },
      {
        "event_id": "EVT-5d356f17435a4cb5b50ce3f6e231effd",
        "event_type": "STEP_ATTEMPT_STARTED",
        "occurred_at": "2026-01-01T00:00:00.067000+00:00",
        "sequence": 8
      },
      {
        "event_id": "EVT-4c48faec70fe4bf78d6b0a5f95d54814",
        "event_type": "OBSERVATION_CAPTURED",
        "occurred_at": "2026-01-01T00:00:00.069000+00:00",
        "sequence": 9
      },
      {
        "event_id": "EVT-947ea0d66a4e423686bac726ee8af093",
        "event_type": "ARTIFACT_SUPPRESSED",
        "occurred_at": "2026-01-01T00:00:00.070000+00:00",
        "sequence": 10
      },
      {
        "event_id": "EVT-9fc832ec415a4110b97bc62b5f9d1e18",
        "event_type": "ASSERTION_EVALUATED",
        "occurred_at": "2026-01-01T00:00:00.071000+00:00",
        "sequence": 11
      },
      {
        "event_id": "EVT-f068be56e48e4077aaa731d7f9ef7f2d",
        "event_type": "STEP_ATTEMPT_COMPLETED",
        "occurred_at": "2026-01-01T00:00:00.073000+00:00",
        "sequence": 12
      },
      {
        "event_id": "EVT-1159be904b174998ae73da4f8c18b2aa",
        "event_type": "TARGET_CLOSED",
        "occurred_at": "2026-01-01T00:00:00.075000+00:00",
        "sequence": 13
      },
      {
        "event_id": "EVT-5ac8a674aa6347c49a187f4af9179101",
        "event_type": "ENVIRONMENT_RELEASED",
        "occurred_at": "2026-01-01T00:00:00.077000+00:00",
        "sequence": 14
      },
      {
        "event_id": "EVT-194e8772197d40a5aaaa86eca72d5e3d",
        "event_type": "RUN_MARKED_INCOMPLETE",
        "occurred_at": "2026-01-01T00:00:00.078000+00:00",
        "sequence": 15
      },
      {
        "event_id": "EVT-e8231540e11d4d65932ba05c0252bf75",
        "event_type": "RUN_COMPLETED",
        "occurred_at": "2026-01-01T00:00:00.079000+00:00",
        "sequence": 16
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
            "sha256": "sha256:a351ebc25efe01882b812758bc0550a6f1c9b94bdae4b81bc9efd4c094f565d5",
            "size_bytes": 574,
            "state": "present"
          }
        ],
        "snapshot_version": "ates-detached-ledger-snapshot-v1"
      },
      "evidence_manifest_path": "manifests/manifest-0001.json",
      "evidence_manifest_sha256": "sha256:71f0422ceb306507e88d025e2529c745c236286f9aab864bca33d9a5c7f5ea3d",
      "evidence_revision": 1,
      "finalization_id": "FINAL-be3df88f06474ee592669ab21347ef85",
      "run_id": "RUN-34765ecacaf14128acb51334a3c81eb9"
    }
