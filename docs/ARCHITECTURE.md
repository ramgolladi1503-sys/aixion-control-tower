# Architecture

## System Overview

```text
Android App
   ↓
Backend API
   ↓
Project Brain + Approval Engine + Risk Engine
   ↓
Agent Runner
   ↓
GitHub Branch / Pull Request / CI
```

## Components

### Mobile App

Primary user interface for idea capture, project visibility, approvals, diffs, risk reviews, test results, and audit history.

### Backend API

The command layer that owns projects, ideas, work orders, approval requests, risk scoring, test records, and audit events.

### Project Brain

Stores structured context and rules for each project. This prevents the product from becoming a generic chat box.

### Approval Engine

Controls approval state transitions and blocks unsafe actions.

### Risk Engine

Classifies proposed changes using file paths, change type, project rules, branch, test plan, rollback plan, and affected area.

### Agent Runner

Future execution worker responsible for patch application, test execution, Git branch handling, and PR creation.

### GitHub Integration

The safe execution layer. Agents must work through branches and pull requests, not direct main edits.

## Core Data Flow

```text
Idea
→ Project Spec
→ Work Order
→ Approval Request
→ Risk Score
→ User Decision
→ Branch Patch
→ Test Run
→ Audit Event
```

## Backend First Principle

The MVP starts with a backend-first implementation. The mobile app can be built against stable APIs after the workflow is correct.

## Safety First Principle

All write operations must be traceable, reversible, and policy checked.
