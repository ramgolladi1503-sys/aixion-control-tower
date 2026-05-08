# Elite Roadmap

## Goal

Move Aixion Control Tower from an MVP approval workflow into a serious AI-agent control system.

## Elite Capabilities

### 1. Approval Intelligence Score

Score every request across:

- Code Risk
- Business Value
- Test Coverage
- Reversibility
- Evidence Strength

### 2. Multi-Agent Review

Use separate reviewer roles:

- builder agent
- reviewer agent
- security agent
- test agent

The final recommendation should include disagreements and missing evidence.

### 3. Self-Critique Gate

Before approval, the agent must answer:

- What could break?
- What assumptions were made?
- What tests prove this works?
- What rollback exists?
- Which files are risky?

### 4. Safe Autonomy Modes

Modes:

- Manual Mode
- Assisted Mode
- Low-Risk Auto Mode
- Strict Mode
- Lockdown Mode

Tradebot and MCP Shield should default to Strict Mode.

### 5. Project Health Score

Score projects using:

- risky pending approvals
- failed tests
- stale branches
- missing docs
- weak test coverage
- security issues
- broken builds

### 6. Daily Briefing

Mobile briefing should summarize:

- what changed
- what is blocked
- what needs approval
- what failed
- what should be done next

### 7. Rollback Intelligence

Every high-risk change should include:

- commit to revert
- files to restore
- tests to rerun
- config hashes
- rollback steps

### 8. Command-to-Branch Execution

The user should be able to command from mobile:

```text
Create a branch for MCP Shield policy engine and prepare the backend skeleton.
```

The system should generate a safe branch plan and ask approval before writing files.

## Elite North Star

Aixion Control Tower should become the personal operating system for controlled AI-assisted project execution.
