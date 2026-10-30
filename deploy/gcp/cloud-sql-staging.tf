# PRE-BILLING DRAFT ONLY. This file defines a staging database but creates no
# resources unless enable_staging_cloud_sql is explicitly set to true.
# Applying it requires billing and owner-reviewed cost, network, secret, and
# recovery decisions. Do not use this staging definition for production.
#
# Validation (from deploy/gcp): terraform fmt -check; terraform init
# -backend=false -input=false -lockfile=readonly; terraform validate.
# Before any approved staging apply, price the selected tier and all related
# resources in asia-south1, complete docs/POSTGRES_BACKUP_RESTORE_RUNBOOK.md,
# set the explicit opt-in and cost-reviewed tier, and review the saved plan.
# This template creates no application schema migration and is not a cutover.

variable "enable_staging_cloud_sql" {
  description = "Explicit opt-in to create the staging Cloud SQL instance. Keep false until billing and owner gates are approved."
  type        = bool
  default     = false
}

variable "staging_cloud_sql_instance_name" {
  description = "Owner-approved staging Cloud SQL instance name."
  type        = string
  default     = "aixion-control-tower-staging-pg"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,96}[a-z0-9]$", var.staging_cloud_sql_instance_name))
    error_message = "staging_cloud_sql_instance_name must be 2-98 lowercase letters, digits, or hyphens and start with a letter."
  }
}

variable "staging_cloud_sql_tier" {
  description = "Cost-reviewed Cloud SQL machine tier for the staging database. Confirm the exact Mumbai-region price before enabling creation."
  type        = string
  default     = "REPLACE_WITH_COST_REVIEWED_TIER"

  validation {
    condition     = !var.enable_staging_cloud_sql || can(regex("^db-custom-[1-9][0-9]*-[1-9][0-9]*$", var.staging_cloud_sql_tier))
    error_message = "When staging Cloud SQL is enabled, staging_cloud_sql_tier must be a cost-reviewed db-custom-<vCPU>-<MiB> machine tier."
  }
}

variable "staging_cloud_sql_disk_size_gb" {
  description = "Initial staging database disk size in GiB."
  type        = number
  default     = 10

  validation {
    condition     = var.staging_cloud_sql_disk_size_gb >= 10 && var.staging_cloud_sql_disk_size_gb <= 100
    error_message = "staging_cloud_sql_disk_size_gb must be between 10 and 100 GiB."
  }
}

variable "staging_cloud_sql_disk_limit_gb" {
  description = "Maximum disk size allowed by automatic storage growth for staging."
  type        = number
  default     = 20

  validation {
    condition     = var.staging_cloud_sql_disk_limit_gb >= var.staging_cloud_sql_disk_size_gb && var.staging_cloud_sql_disk_limit_gb <= 100
    error_message = "staging_cloud_sql_disk_limit_gb must be at least the initial size and no more than 100 GiB."
  }
}

variable "staging_cloud_sql_backup_start_time" {
  description = "UTC start time for the daily staging backup window (HH:MM)."
  type        = string
  default     = "20:00"

  validation {
    condition     = can(regex("^([01][0-9]|2[0-3]):[0-5][0-9]$", var.staging_cloud_sql_backup_start_time))
    error_message = "staging_cloud_sql_backup_start_time must be a UTC time in HH:MM format."
  }
}

variable "staging_cloud_sql_log_retention_days" {
  description = "PostgreSQL PITR transaction-log retention days for the Enterprise edition staging instance."
  type        = number
  default     = 6

  validation {
    condition     = var.staging_cloud_sql_log_retention_days >= 1 && var.staging_cloud_sql_log_retention_days <= 7
    error_message = "Enterprise edition PostgreSQL PITR log retention must be between 1 and 7 days."
  }
}

resource "google_sql_database_instance" "staging" {
  count               = var.enable_staging_cloud_sql ? 1 : 0
  project             = var.project_id
  name                = var.staging_cloud_sql_instance_name
  region              = var.cloud_run_region
  database_version    = "POSTGRES_17"
  deletion_protection = true

  settings {
    tier                  = var.staging_cloud_sql_tier
    availability_type     = "ZONAL"
    disk_type             = "PD_SSD"
    disk_size             = var.staging_cloud_sql_disk_size_gb
    disk_autoresize       = true
    disk_autoresize_limit = var.staging_cloud_sql_disk_limit_gb

    user_labels = {
      application = "aixion-control-tower"
      environment = "staging"
      managed_by  = "terraform"
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = var.staging_cloud_sql_backup_start_time
      transaction_log_retention_days = var.staging_cloud_sql_log_retention_days

      # Keep one more daily backup than the PITR log window so a backup exists
      # for the full configured transaction-log period.
      backup_retention_settings {
        retained_backups = var.staging_cloud_sql_log_retention_days + 1
        retention_unit   = "COUNT"
      }
    }

    ip_configuration {
      # Cloud Run connects through the Cloud SQL connector. No authorized
      # networks are configured; connector TLS is mandatory.
      ipv4_enabled          = true
      ssl_mode              = "ENCRYPTED_ONLY"
      connector_enforcement = "REQUIRED"
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_sql_database" "staging" {
  count      = var.enable_staging_cloud_sql ? 1 : 0
  project    = var.project_id
  name       = "aixion_control_tower"
  instance   = google_sql_database_instance.staging[0].name
  charset    = "UTF8"
  collation  = "en_US.UTF8"
  depends_on = [google_sql_database_instance.staging]
}

output "staging_cloud_sql_connection_name" {
  description = "Cloud SQL connection name for the staging Cloud Run Cloud SQL connector annotation."
  value       = var.enable_staging_cloud_sql ? google_sql_database_instance.staging[0].connection_name : null
}
