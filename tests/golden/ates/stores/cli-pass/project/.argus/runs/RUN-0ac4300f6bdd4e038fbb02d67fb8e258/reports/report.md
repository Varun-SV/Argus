# Argus ATES Test Execution Report

**Run:** `RUN-0ac4300f6bdd4e038fbb02d67fb8e258`
**Canonical status:** `passed`
**Evidence trust:** `bound_verified`
**Report trust:** `unverified_derived`

> Derived view only; canonical ATES evidence remains authoritative.

## Execution / source

    {
      "adapter_type": "cli",
      "argus_version": "0.1.1.dev705+gf6ff07581.d20261107",
      "configuration_commitment": {
        "canonicalization_profile": "ates-runtime-config-v1",
        "method": "hmac-sha256",
        "value": "hmac:73a729a9d576bcb52b5726b0b9b357f4b5a9a99105808ffb55279c192ca3d229",
        "verification_ref": "protected://ates-runtime-identity-key"
      },
      "environment_type": "local",
      "evidence_profile": "ates-privacy-v1",
      "execution_kind": "scripted",
      "model": "MODEL-d7d72e854872fb97d9bf8899",
      "model_provider": "ollama",
      "provider": "ollama",
      "run_id": "RUN-0ac4300f6bdd4e038fbb02d67fb8e258",
      "source": {
        "commitment": {
          "canonicalization_profile": "ates-runtime-scripted-source-v1",
          "method": "hmac-sha256",
          "value": "hmac:f52cba79d3808c519f9a31861a1eeffe9f012f9cb4bcf1752578132e29bd0465",
          "verification_ref": "protected://ates-runtime-identity-key"
        },
        "kind": "test_spec",
        "test_case_id": "TEST-24f1a6e0f4c4ea96c0a72eb5"
      },
      "started_at": "2026-01-01T00:00:00.006000+00:00"
    }

## Outcome

    {
      "authorization": null,
      "correction_ids": [],
      "effective_status": "passed",
      "evidence_revision": 1,
      "finalization_id": "FINAL-43e278e89bf64b77b9715e95f824dd0c",
      "finalized_at": "2026-01-01T00:00:00.034000+00:00",
      "revision": 1,
      "run_id": "RUN-0ac4300f6bdd4e038fbb02d67fb8e258",
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
        "step_id": "STEP-4260a4a119614f5b9cb7a8a3c4393c02"
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
        "step_id": "STEP-ed13a63b4a3b4d7098c483255d1c22e2"
      }
    ]

## Attempt history

    [
      {
        "attempt": 1,
        "ended_at": "2026-01-01T00:00:00.017000+00:00",
        "retry_reason": null,
        "started_at": "2026-01-01T00:00:00.012000+00:00",
        "status": "passed",
        "step_attempt_id": "STEPATT-0a3cb5f7c34e46b8a10e59c92f8ba9d2",
        "step_id": "STEP-4260a4a119614f5b9cb7a8a3c4393c02"
      },
      {
        "attempt": 1,
        "ended_at": "2026-01-01T00:00:00.027000+00:00",
        "retry_reason": null,
        "started_at": "2026-01-01T00:00:00.022000+00:00",
        "status": "passed",
        "step_attempt_id": "STEPATT-ec27602d040e4bdba08810683128fed5",
        "step_id": "STEP-ed13a63b4a3b4d7098c483255d1c22e2"
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
        "assertion_id": "ASSERT-51126ff402d44d528db07e3ec9584e0d",
        "expected": {
          "disposition": "redacted",
          "protected_ref": null,
          "reason": "privacy.assertion_value",
          "secret_refs": [],
          "value": "<redacted>"
        },
        "kind": "exit_code_is",
        "method": "deterministic.adapter_observation",
        "observation_id": "OBS-f1a993bdfb9042a990ef3ac00d2364eb",
        "required": true,
        "requirement": null,
        "result": "passed",
        "step_attempt_id": "STEPATT-0a3cb5f7c34e46b8a10e59c92f8ba9d2",
        "step_id": "STEP-4260a4a119614f5b9cb7a8a3c4393c02"
      },
      {
        "actual": {
          "disposition": "suppressed",
          "protected_ref": null,
          "reason": "privacy.target_generated",
          "secret_refs": [],
          "value": null
        },
        "assertion_id": "ASSERT-d24e8b6d263f4fe08a37ffb80ed103b1",
        "expected": {
          "disposition": "redacted",
          "protected_ref": null,
          "reason": "privacy.assertion_value",
          "secret_refs": [],
          "value": "<redacted>"
        },
        "kind": "stdout_contains",
        "method": "deterministic.adapter_observation",
        "observation_id": "OBS-1eb9eccbc25442d8b41f4cc0ebb96d5b",
        "required": true,
        "requirement": null,
        "result": "passed",
        "step_attempt_id": "STEPATT-ec27602d040e4bdba08810683128fed5",
        "step_id": "STEP-ed13a63b4a3b4d7098c483255d1c22e2"
      }
    ]

## Observations

    [
      {
        "capture_policy": "ates-privacy-v1",
        "captured_at": "2026-01-01T00:00:00.014000+00:00",
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
        "observation_id": "OBS-f1a993bdfb9042a990ef3ac00d2364eb",
        "source": "cli",
        "step_attempt_id": "STEPATT-0a3cb5f7c34e46b8a10e59c92f8ba9d2"
      },
      {
        "capture_policy": "ates-privacy-v1",
        "captured_at": "2026-01-01T00:00:00.024000+00:00",
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
        "observation_id": "OBS-1eb9eccbc25442d8b41f4cc0ebb96d5b",
        "source": "cli",
        "step_attempt_id": "STEPATT-ec27602d040e4bdba08810683128fed5"
      }
    ]

## Artifacts (inert references only)

    []

## Findings

    []

## Failures / ambiguous outcomes

    []

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
          "audit_id": "AUDIT-28812ed61cfc4b9ba91cbead109bb828",
          "dedupe_key": "finalization:FINAL-43e278e89bf64b77b9715e95f824dd0c",
          "details": {
            "effective_status": "passed",
            "evidence_revision": 1,
            "finalization_id": "FINAL-43e278e89bf64b77b9715e95f824dd0c",
            "manifest_digest": "sha256:85a5564bbcf735420e5e37783676faa71855f6271b6ada88c523d32be2af74ff",
            "revision": 1,
            "run_id": "RUN-0ac4300f6bdd4e038fbb02d67fb8e258"
          },
          "event_type": "finalization.bound",
          "ledger_version": "ates-audit-ledger-v1",
          "occurred_at": "2026-01-01T00:00:00.034000+00:00",
          "previous_record_digest": null
        }
      ],
      "trust_note": "The local chain is not an external tamper-evidence boundary."
    }

## Timeline

    [
      {
        "event_id": "EVT-3ba16b4f8cf948da9d5f17e5ef0f2723",
        "event_type": "RUN_STARTED",
        "occurred_at": "2026-01-01T00:00:00.007000+00:00",
        "sequence": 1
      },
      {
        "event_id": "EVT-ea10fd02d7df4ca790da597101032bfa",
        "event_type": "ENVIRONMENT_PREPARED",
        "occurred_at": "2026-01-01T00:00:00.008000+00:00",
        "sequence": 2
      },
      {
        "event_id": "EVT-5c27fc8f595d406696c390779fab45b3",
        "event_type": "TARGET_LAUNCHED",
        "occurred_at": "2026-01-01T00:00:00.009000+00:00",
        "sequence": 3
      },
      {
        "event_id": "EVT-c8fee17825f4494ca65bfa7dc420c039",
        "event_type": "STEP_ATTEMPT_STARTED",
        "occurred_at": "2026-01-01T00:00:00.013000+00:00",
        "sequence": 4
      },
      {
        "event_id": "EVT-98741f5ed68a47b489376d10eb3c6fd1",
        "event_type": "OBSERVATION_CAPTURED",
        "occurred_at": "2026-01-01T00:00:00.015000+00:00",
        "sequence": 5
      },
      {
        "event_id": "EVT-05323b2cbff14bbba87e1a23598261a6",
        "event_type": "ASSERTION_EVALUATED",
        "occurred_at": "2026-01-01T00:00:00.016000+00:00",
        "sequence": 6
      },
      {
        "event_id": "EVT-b45b524f744b44af8f75a4f994beb0a1",
        "event_type": "STEP_ATTEMPT_COMPLETED",
        "occurred_at": "2026-01-01T00:00:00.018000+00:00",
        "sequence": 7
      },
      {
        "event_id": "EVT-475f253073d34519b8f061a6d23c0c77",
        "event_type": "STEP_ATTEMPT_STARTED",
        "occurred_at": "2026-01-01T00:00:00.023000+00:00",
        "sequence": 8
      },
      {
        "event_id": "EVT-e52dbfee00a940cb929bf13736915659",
        "event_type": "OBSERVATION_CAPTURED",
        "occurred_at": "2026-01-01T00:00:00.025000+00:00",
        "sequence": 9
      },
      {
        "event_id": "EVT-1606df39bce441079179bdd5c32453b3",
        "event_type": "ASSERTION_EVALUATED",
        "occurred_at": "2026-01-01T00:00:00.026000+00:00",
        "sequence": 10
      },
      {
        "event_id": "EVT-33c6d64f076d4513a317fe611f649369",
        "event_type": "STEP_ATTEMPT_COMPLETED",
        "occurred_at": "2026-01-01T00:00:00.028000+00:00",
        "sequence": 11
      },
      {
        "event_id": "EVT-0c8db1852ffe4fae9a1e040e924b9bf5",
        "event_type": "TARGET_CLOSED",
        "occurred_at": "2026-01-01T00:00:00.030000+00:00",
        "sequence": 12
      },
      {
        "event_id": "EVT-af8d56e3465a4333be43cf62675d4098",
        "event_type": "ENVIRONMENT_RELEASED",
        "occurred_at": "2026-01-01T00:00:00.032000+00:00",
        "sequence": 13
      },
      {
        "event_id": "EVT-e0d53927d4fe4c3cbf352ce5ddaa457e",
        "event_type": "RUN_MARKED_INCOMPLETE",
        "occurred_at": "2026-01-01T00:00:00.033000+00:00",
        "sequence": 14
      },
      {
        "event_id": "EVT-68c33b8722304ddcb980753d3891f229",
        "event_type": "RUN_COMPLETED",
        "occurred_at": "2026-01-01T00:00:00.034000+00:00",
        "sequence": 15
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
            "sha256": "sha256:188fbdb8dfdb399fd7e1d223dc01338a738b44658b41a16c58c6c42926577a4a",
            "size_bytes": 574,
            "state": "present"
          }
        ],
        "snapshot_version": "ates-detached-ledger-snapshot-v1"
      },
      "evidence_manifest_path": "manifests/manifest-0001.json",
      "evidence_manifest_sha256": "sha256:85a5564bbcf735420e5e37783676faa71855f6271b6ada88c523d32be2af74ff",
      "evidence_revision": 1,
      "finalization_id": "FINAL-43e278e89bf64b77b9715e95f824dd0c",
      "run_id": "RUN-0ac4300f6bdd4e038fbb02d67fb8e258"
    }
