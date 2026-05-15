# Worker Execution Policy and Artifact Evidence

This document describes the PR 103 worker validation safety and evidence layer.

## Purpose

Approved AgentTask execution must not only run validation. It must prove what policy allowed the command, how long execution took, what output was captured, and whether output was truncated.

## Policy version

Current policy:

```text
aixion-validation-execution-policy-v1
```

## Core policy rules

The validation runner records and enforces:

```text
allowed command prefixes
blocked shell-control tokens
blocked dangerous substrings
maximum validation command count
maximum command length
shell execution disabled
network fetch commands disabled
```

## Artifact evidence

Each executed validation result includes:

```text
command
exit_code
timed_out
output_summary
duration_ms
output_truncated
output_chars
allowed_prefix
```

The finished validation event includes an aggregate artifact summary:

```text
artifact_type
command_count
passed_count
failed_count
timed_out_count
total_duration_ms
output_truncated
max_output_chars
```

## Output cap

Validation output summaries are capped at 4000 characters. If output is larger, the runner keeps the tail and marks `output_truncated=true`.

## Refusal evidence

When validation is refused before execution, the audit event records:

```text
agent_worker.validation_run_refused
reason
validation_policy
```

No validation command is executed after refusal.

## Remaining gap

This is execution-policy evidence, not full sandboxing. The next production step is containerized execution with resource limits, network policy, and attached log artifacts.
