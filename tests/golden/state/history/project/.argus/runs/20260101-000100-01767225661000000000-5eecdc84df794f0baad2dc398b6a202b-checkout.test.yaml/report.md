# Argus Test Report

- **Test:** `checkout`
- **Adapter:** `desktop`
- **Environment:** `capsule`
- **Isolated:** yes
- **Location:** `capsule/hyperv`
- **Provider:** `ollama`
- **Status:** FAIL
- **Duration:** 3.9s
- **Tokens:** 950 (2 LLM calls)
- **Staged files:** 0
- **Collected artifacts:** 0
- **Failure Capsule:** `golden-failure-1`
- **Failure VM:** `argus-golden-vm`
- **Failure state:** `Off`
- **Failure storage:** `capsules/golden-failure-1`

| # | Step | Kind | Status | Duration |
|---|------|------|--------|----------|
| 1 | launch the app | launch | ✅ pass | 1.0s |
| 2 | text 'Order placed' is visible | assert | ❌ fail | 2.0s |
| 3 | close the app | teardown | ✅ pass | 0.5s |

## Failure Capsule

Argus retained the Capsule before teardown so the failed VM disk/configuration can be inspected or reproduced.

- **Reason:** test failure
- **VM:** `argus-golden-vm`
- **State:** `Off`
- **Storage:** `capsules/golden-failure-1`

## Steps

### Step 1: launch the app

**Status:** ✅ `pass`  
**Kind:** launch  
**Duration:** 1.0s

### Step 2: text 'Order placed' is visible

**Status:** ❌ `fail`  
**Kind:** assert  
**Duration:** 2.0s
  
**Expected:** Order placed
  
**Actual:** Payment declined
  
**Actions taken:**
  - click 7

![step 2 screenshot](screenshots/step-2.png)

### Step 3: close the app

**Status:** ✅ `pass`  
**Kind:** teardown  
**Duration:** 0.5s
