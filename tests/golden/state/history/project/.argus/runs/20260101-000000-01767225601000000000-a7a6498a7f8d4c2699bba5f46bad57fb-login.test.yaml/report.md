# Argus Test Report

- **Test:** `login`
- **Adapter:** `desktop`
- **Environment:** `direct`
- **Isolated:** no
- **Location:** `unknown`
- **Provider:** `ollama`
- **Status:** PASS
- **Duration:** 5.3s
- **Tokens:** 1500 (4 LLM calls)
- **Staged files:** 0
- **Collected artifacts:** 0

| # | Step | Kind | Status | Duration |
|---|------|------|--------|----------|
| 1 | launch the app | launch | ✅ pass | 1.2s |
| 2 | sign in as the golden user | act | ✅ pass | 3.5s |
| 3 | window title contains 'Dashboard' | assert | ✅ pass | 0.2s |

## Steps

### Step 1: launch the app

**Status:** ✅ `pass`  
**Kind:** launch  
**Duration:** 1.2s
  
**Actions taken:**
  - launch app.exe

### Step 2: sign in as the golden user

**Status:** ✅ `pass`  
**Kind:** act  
**Duration:** 3.5s
  
**Actions taken:**
  - click 4
  - type 'golden-user'
  - key enter

### Step 3: window title contains 'Dashboard'

**Status:** ✅ `pass`  
**Kind:** assert  
**Duration:** 0.2s
  
**Expected:** Dashboard
  
**Actual:** Dashboard - Golden App
