# PRE-BILLING DRAFT ONLY.
# This isolated production root reserves billable global networking resources
# only after explicit opt-in. Do not enable the guard until the PostgreSQL
# production gate, reviewed cost estimate, remote Terraform state, deployed
# Cloud Run service, IAM, and owner approvals are complete.
#
# This file does not deploy the API or configure DNS. The managed certificate
# becomes active only after api.aixionlab.com resolves to the output IP.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0, < 8.0"
    }
  }
}

variable "project_id" {
  description = "Explicit production Google Cloud project ID. No staging default is allowed."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be an explicit valid Google Cloud project ID."
  }
}

variable "cloud_run_region" {
  description = "Production Cloud Run region selected by the owner."
  type        = string
  default     = "asia-south1"

  validation {
    condition     = var.cloud_run_region == "asia-south1"
    error_message = "This production deployment is configured for the owner-selected Mumbai region asia-south1."
  }
}

variable "cloud_run_service_name" {
  description = "Name of the already-deployed production Cloud Run API service."
  type        = string

  validation {
    condition     = can(regex("^[a-z]([-a-z0-9]{0,47}[a-z0-9])?$", var.cloud_run_service_name))
    error_message = "cloud_run_service_name must be a valid Cloud Run service name."
  }
}

variable "api_domain" {
  description = "Production API hostname requested for the Aixion Control Tower app."
  type        = string
  default     = "api.aixionlab.com"

  validation {
    condition     = var.api_domain == "api.aixionlab.com"
    error_message = "The production HTTPS endpoint must use api.aixionlab.com."
  }
}

variable "enable_production_load_balancer" {
  description = "Explicit cost-bearing opt-in. Keep false until all production and billing gates are approved."
  type        = bool
  default     = false
}

provider "google" {
  project = var.project_id
  region  = var.cloud_run_region
}

resource "terraform_data" "production_apply_guard" {
  input = var.enable_production_load_balancer

  lifecycle {
    precondition {
      condition     = var.enable_production_load_balancer
      error_message = "Production load-balancer resources are disabled until the owner explicitly enables them after all gates pass."
    }
  }
}

resource "google_compute_global_address" "api" {
  project      = var.project_id
  name         = "aixion-api-production-ip"
  address_type = "EXTERNAL"
  ip_version   = "IPV4"

  depends_on = [terraform_data.production_apply_guard]
}

resource "google_compute_region_network_endpoint_group" "api" {
  project               = var.project_id
  name                  = "aixion-api-production-neg"
  region                = var.cloud_run_region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = var.cloud_run_service_name
  }

  depends_on = [terraform_data.production_apply_guard]
}

resource "google_compute_backend_service" "api" {
  project               = var.project_id
  name                  = "aixion-api-production-backend"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTP"
  timeout_sec           = 60

  backend {
    group = google_compute_region_network_endpoint_group.api.id
  }

  depends_on = [terraform_data.production_apply_guard]
}

resource "google_compute_url_map" "api" {
  project         = var.project_id
  name            = "aixion-api-production-url-map"
  default_service = google_compute_backend_service.api.id

  depends_on = [terraform_data.production_apply_guard]
}

resource "google_compute_managed_ssl_certificate" "api" {
  project = var.project_id
  name    = "aixion-api-production-certificate"

  managed {
    domains = [var.api_domain]
  }

  depends_on = [terraform_data.production_apply_guard]
}

resource "google_compute_ssl_policy" "api" {
  project         = var.project_id
  name            = "aixion-api-production-tls-policy"
  profile         = "MODERN"
  min_tls_version = "TLS_1_2"

  depends_on = [terraform_data.production_apply_guard]
}

resource "google_compute_target_https_proxy" "api" {
  project          = var.project_id
  name             = "aixion-api-production-https-proxy"
  url_map          = google_compute_url_map.api.id
  ssl_certificates = [google_compute_managed_ssl_certificate.api.id]
  ssl_policy       = google_compute_ssl_policy.api.id

  depends_on = [terraform_data.production_apply_guard]
}

resource "google_compute_global_forwarding_rule" "api_https" {
  project               = var.project_id
  name                  = "aixion-api-production-https-forwarding-rule"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  network_tier          = "PREMIUM"
  ip_protocol           = "TCP"
  port_range            = "443"
  ip_address            = google_compute_global_address.api.id
  target                = google_compute_target_https_proxy.api.id

  depends_on = [terraform_data.production_apply_guard]
}

output "api_load_balancer_ipv4" {
  description = "Publish this as the api.aixionlab.com Spaceship A record only after owner review."
  value       = google_compute_global_address.api.address
}
