# PRE-BILLING DRAFT ONLY. Do not apply until Slice 1 and all owner gates pass.
# Schedules intentionally remain unresolved; cadence must come from an approved
# workload estimate. No Terraform apply or cloud access is authorized here.

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0, < 8.0"
    }
  }
}

variable "project_id" {
  description = "Owner-approved dedicated staging project ID."
  type        = string
  default     = "REPLACE_WITH_PROJECT_ID"
}

variable "cloud_run_region" {
  description = "Owner-approved region matching the Cloud Run Job manifests."
  type        = string
  default     = "REPLACE_WITH_REGION"
}

variable "scheduler_region" {
  description = "Owner-approved Cloud Scheduler region."
  type        = string
  default     = "REPLACE_WITH_SCHEDULER_REGION"
}

variable "push_schedule" {
  description = "Owner-approved cron cadence derived from the staged workload/SLO."
  type        = string
  default     = "REPLACE_WITH_PUSH_CRON"
}

variable "email_schedule" {
  description = "Owner-approved cron cadence derived from the staged workload/SLO."
  type        = string
  default     = "REPLACE_WITH_EMAIL_CRON"
}

variable "scheduler_timezone" {
  description = "Owner-approved IANA timezone used to interpret both schedules."
  type        = string
  default     = "REPLACE_WITH_IANA_TIMEZONE"
}

variable "scheduler_service_account_id" {
  description = "Dedicated, non-human service account ID used only to invoke the two jobs."
  type        = string
  default     = "REPLACE_WITH_SCHEDULER_SERVICE_ACCOUNT_ID"
}

variable "firebase_project_id" {
  description = "Owner-approved Firebase project ID used by the Android app and push dispatcher."
  type        = string
  default     = "REPLACE_WITH_FIREBASE_PROJECT_ID"
}

variable "push_runtime_service_account_email" {
  description = "The dedicated Cloud Run push job runtime service account; never the scheduler identity."
  type        = string
  default     = "REPLACE_WITH_PUSH_RUNTIME_SERVICE_ACCOUNT"
}

provider "google" {
  project = var.project_id
  region  = var.cloud_run_region
}

resource "google_service_account" "scheduler_invoker" {
  project      = var.project_id
  account_id   = var.scheduler_service_account_id
  display_name = "Aixion staging Cloud Run job scheduler invoker"
  description  = "Invokes only the push and email Cloud Run jobs via Cloud Scheduler."
}

resource "google_cloud_run_v2_job_iam_member" "push_invoker" {
  project  = var.project_id
  location = var.cloud_run_region
  name     = "aixion-push-dispatch"
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_invoker.email}"
}

resource "google_cloud_run_v2_job_iam_member" "email_invoker" {
  project  = var.project_id
  location = var.cloud_run_region
  name     = "aixion-email-dispatch"
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_invoker.email}"
}

# The push worker needs only the FCM send operation. Keep this project-level
# custom role separate from the scheduler's job-invoker identity and from the
# email runtime identity. Template only: owner approval and Slice 1 precede apply.
resource "google_project_iam_custom_role" "fcm_sender" {
  project     = var.firebase_project_id
  role_id     = "aixionControlTowerPushSender"
  title       = "Aixion Control Tower push sender"
  description = "Sends approval push notifications through Firebase Cloud Messaging."
  permissions = ["cloudmessaging.messages.create"]
  stage       = "GA"
}

resource "google_project_iam_member" "fcm_push_sender" {
  project = var.firebase_project_id
  role    = google_project_iam_custom_role.fcm_sender.name
  member  = "serviceAccount:${var.push_runtime_service_account_email}"
}

resource "google_cloud_scheduler_job" "push_dispatch" {
  project          = var.project_id
  region           = var.scheduler_region
  name             = "aixion-push-dispatch"
  description      = "PRE-BILLING DRAFT ONLY: triggers one bounded push outbox attempt."
  paused           = true
  schedule         = var.push_schedule
  time_zone        = var.scheduler_timezone
  attempt_deadline = "300s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.cloud_run_region}/jobs/aixion-push-dispatch:run"
    body        = base64encode("{}")
    headers = {
      "Content-Type" = "application/json"
    }

    oauth_token {
      service_account_email = google_service_account.scheduler_invoker.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
}

resource "google_cloud_scheduler_job" "email_dispatch" {
  project          = var.project_id
  region           = var.scheduler_region
  name             = "aixion-email-dispatch"
  description      = "PRE-BILLING DRAFT ONLY: triggers one bounded email outbox attempt."
  paused           = true
  schedule         = var.email_schedule
  time_zone        = var.scheduler_timezone
  attempt_deadline = "300s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.cloud_run_region}/jobs/aixion-email-dispatch:run"
    body        = base64encode("{}")
    headers = {
      "Content-Type" = "application/json"
    }

    oauth_token {
      service_account_email = google_service_account.scheduler_invoker.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
}
