# Aixion Control Tower Android

Android-first mobile command and approval console for AI-agent project execution.

## Current MVP State

This is a Jetpack Compose Android MVP shell with backend-aware screens, repository fallback, and real navigation.

Implemented screens:

- Home Dashboard
- Projects
- Work Orders
- Command
- Approval Inbox
- Approval Detail
- Diff Viewer
- Test Runs
- Audit Trail

## Backend Connection

The app defaults to the Android emulator host URL:

```text
http://10.0.2.2:8000/
```

Run the FastAPI backend locally from the repo root:

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:app --reload
```

## Run Locally

Open `mobile/android` in Android Studio and run the `app` module.

The app uses repository fallback. If the backend is unavailable, core screens fall back to mock data instead of going blank.

## CI Build Verification

GitHub Actions uses a managed Gradle installation to run:

```bash
gradle assembleDebug
```

A committed Gradle wrapper should be generated locally with:

```bash
cd mobile/android
gradle wrapper --gradle-version 8.10.2
```

Then commit:

```text
gradlew
gradlew.bat
gradle/wrapper/gradle-wrapper.jar
gradle/wrapper/gradle-wrapper.properties
```

The binary wrapper jar is intentionally not fabricated by hand.

## Design Direction

The UI uses a dark command-center style inspired by dense operational dashboards, adapted for AI-agent control instead of trading.

## MVP Rule

The app must never become a blind approval button. Risk, diff, test evidence, and required actions must remain visible before any meaningful approval.
