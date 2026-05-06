# Aixion Control Tower Android

Android-first mobile command and approval console for AI-agent project execution.

## Current MVP State

This is a Jetpack Compose starter shell with mock data and the first operator-grade screens:

- Home Dashboard
- Projects
- Command
- Approval Inbox
- Audit Trail

## Design Direction

The UI uses a dark command-center style inspired by dense operational dashboards, adapted for AI-agent control instead of trading.

## Run Locally

Open `mobile/android` in Android Studio and run the `app` module.

## Next Implementation Steps

1. Add Retrofit API client for the FastAPI backend.
2. Replace mock data with repository-backed state.
3. Add Approval Detail screen.
4. Add Diff Viewer screen.
5. Add Work Orders screen.
6. Add Test Runs screen.
7. Add notification deep links.

## MVP Rule

The app must never become a blind approval button. Risk, diff, test evidence, and required actions must remain visible before any meaningful approval.
