# Argus Test Report

- **Test:** `settings`
- **Adapter:** `desktop`
- **Environment:** `direct`
- **Isolated:** no
- **Location:** `unknown`
- **Provider:** `ollama`
- **Status:** ERROR
- **Duration:** 1.1s
- **Tokens:** 0 (0 LLM calls)
- **Staged files:** 1
- **Collected artifacts:** 1
- **Failure Capsule retention:** ⚠️ retention failed; Capsule preserved for recovery
- **Recovery VM:** `argus-golden-vm-2`
- **Recovery storage:** `capsules/golden-failure-2`

| # | Step | Kind | Status | Duration |
|---|------|------|--------|----------|
| 1 | launch the app | launch | 💥 error | 0.8s |
| 2 | open settings | act | ⏭ skipped | 0.0s |

**Error:** RuntimeError: process exited with code 3

**Transfer error:** artifact collection failed: guest path missing

## Staged files

| Source | Guest destination | Size | SHA-256 |
|---|---|---:|---|
| `fixtures/input.txt` | `C:\ArgusStage\input.txt` | 12 | `0000000000000000000000000000000000000000000000000000000000000000` |

## Collected artifacts

| Guest path | Host artifact | Size | SHA-256 |
|---|---|---:|---|
| `C:\ArgusStage\out.log` | `artifacts/out.log` | 34 | `ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff` |

## Failure Capsule retention error

Argus could not complete the requested retention operation. To avoid destroying evidence, the Capsule was left registered and its session storage was preserved.

- **Error:** retention timed out
- **VM:** `argus-golden-vm-2`
- **Storage:** `capsules/golden-failure-2`
- **Recovery:** inspect the preserved Capsule manually

## Steps

### Step 1: launch the app

**Status:** 💥 `error`  
**Kind:** launch  
**Duration:** 0.8s
  
**Note:** adapter error: process exited with code 3

### Step 2: open settings

**Status:** ⏭ `skipped`  
**Kind:** act  
**Duration:** 0.0s
  
**Note:** skipped after failure (continue_on_failure: false)
