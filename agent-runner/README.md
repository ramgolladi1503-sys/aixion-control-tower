# Agent Runner

The agent runner will become the controlled execution worker for Aixion Control Tower.

## Future Responsibilities

- receive approved work orders
- apply approved patches to feature branches
- run configured tests
- capture logs and summaries
- create commits
- open pull requests
- report status back to backend

## MVP Status

The MVP backend stores approvals and test records. The runner is intentionally documented but not active yet because uncontrolled execution would be unsafe at this stage.

## Hard Rules

- never modify `main` directly
- never touch secrets
- never apply blocked approvals
- never run production deployment commands in MVP
- always write audit events through the backend
