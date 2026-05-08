# Android App Structure — Kotlin + Jetpack Compose

## Direction

Aixion Control Tower should be Android-first, operator-grade, and command-center styled. The mobile app must not feel like a generic chatbot or basic GitHub client.

## Technology Choice

```text
Language: Kotlin
UI: Jetpack Compose
Architecture: MVVM + Repository pattern
Networking: Retrofit/OkHttp
State: Kotlin Flow / StateFlow
Navigation: Compose Navigation
Storage: Room later; in-memory state for first MVP
Notifications: Firebase Cloud Messaging later
```

## Module Structure

```text
mobile/android/
├── app/
│   ├── build.gradle.kts
│   └── src/main/
│       ├── AndroidManifest.xml
│       └── java/com/aixion/controltower/
│           ├── MainActivity.kt
│           ├── ControlTowerApp.kt
│           ├── navigation/
│           │   ├── Routes.kt
│           │   └── AppNavGraph.kt
│           ├── core/
│           │   ├── api/
│           │   │   ├── ApiClient.kt
│           │   │   └── ControlTowerApi.kt
│           │   ├── model/
│           │   │   ├── ProjectModels.kt
│           │   │   ├── ApprovalModels.kt
│           │   │   ├── WorkOrderModels.kt
│           │   │   └── AuditModels.kt
│           │   └── ui/
│           │       ├── theme/
│           │       │   ├── Color.kt
│           │       │   ├── Theme.kt
│           │       │   └── Type.kt
│           │       └── components/
│           │           ├── RiskBadge.kt
│           │           ├── StatusCard.kt
│           │           ├── ApprovalCard.kt
│           │           ├── ProjectCard.kt
│           │           └── DiffBlock.kt
│           ├── feature/
│           │   ├── home/
│           │   │   ├── HomeScreen.kt
│           │   │   └── HomeViewModel.kt
│           │   ├── projects/
│           │   │   ├── ProjectsScreen.kt
│           │   │   └── ProjectsViewModel.kt
│           │   ├── command/
│           │   │   ├── CommandChatScreen.kt
│           │   │   └── CommandChatViewModel.kt
│           │   ├── approvals/
│           │   │   ├── ApprovalInboxScreen.kt
│           │   │   ├── ApprovalDetailScreen.kt
│           │   │   └── ApprovalsViewModel.kt
│           │   ├── diff/
│           │   │   └── DiffViewerScreen.kt
│           │   ├── workorders/
│           │   │   └── WorkOrdersScreen.kt
│           │   ├── tests/
│           │   │   └── TestRunsScreen.kt
│           │   └── audit/
│           │       └── AuditTrailScreen.kt
│           └── data/
│               ├── repository/
│               │   ├── ProjectRepository.kt
│               │   ├── ApprovalRepository.kt
│               │   ├── WorkOrderRepository.kt
│               │   └── AuditRepository.kt
│               └── mock/
│                   └── MockData.kt
├── settings.gradle.kts
└── README.md
```

## Bottom Navigation

MVP bottom navigation:

```text
Home
Projects
Command
Inbox
Audit
```

## MVP Screen Build Order

1. Home
2. Projects
3. Command Chat
4. Approval Inbox
5. Approval Detail
6. Diff Viewer
7. Work Orders
8. Test Runs
9. Audit Trail

## Design Language

Borrow the dense operational style from Algotradify-style dashboards, but adapt it for AI-agent control:

- dark theme first
- strong risk badges
- compact cards
- high information density
- quick scan dashboard
- clear approve/reject flows
- no playful chatbot UI

## Non-Negotiable Mobile UX Rules

- High-risk approvals must open detail view before approval.
- Blocked requests cannot show an approve button.
- Diff must be visible before approving file changes.
- Required actions must be shown above action buttons.
- Risk reasons must never be hidden.
