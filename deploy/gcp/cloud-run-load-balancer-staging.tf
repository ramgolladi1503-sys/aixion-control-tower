# PRE-BILLING DRAFT ONLY. Do not apply until Slice 1 and all owner gates pass.
# Create only after the reviewed Cloud Run staging service exists. This draft
# intentionally configures HTTPS only; HTTP redirect can be added after review.
# It reserves billable global networking resources when applied.

variable "api_domain" {
  description = "Owner-approved API hostname served by the HTTPS load balancer."
  type        = string
  default     = "api.aixionlab.com"

  validation {
    condition = length(var.api_domain) <= 253 && length(split(".", var.api_domain)) >= 2 && alltrue([
      for label in split(".", var.api_domain) : can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", label))
    ])
    error_message = "api_domain must be a valid lowercase DNS hostname with valid labels."
  }
}

variable "api_cloud_run_service_name" {
  description = "Name of the already-deployed Cloud Run staging API service."
  type        = string
  default     = "aixion-control-tower-staging"

  validation {
    condition     = can(regex("^[a-z]([-a-z0-9]{0,47}[a-z0-9])?$", var.api_cloud_run_service_name))
    error_message = "api_cloud_run_service_name must be a valid Cloud Run service name."
  }
}

resource "google_compute_global_address" "api" {
  project      = var.project_id
  name         = "aixion-api-staging-ip"
  address_type = "EXTERNAL"
  ip_version   = "IPV4"
}

resource "google_compute_region_network_endpoint_group" "api" {
  project               = var.project_id
  name                  = "aixion-api-staging-neg"
  region                = var.cloud_run_region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = var.api_cloud_run_service_name
  }
}

resource "google_compute_backend_service" "api" {
  project               = var.project_id
  name                  = "aixion-api-staging-backend"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTP"
  timeout_sec           = 60

  backend {
    group = google_compute_region_network_endpoint_group.api.id
  }
}

resource "google_compute_url_map" "api" {
  project         = var.project_id
  name            = "aixion-api-staging-url-map"
  default_service = google_compute_backend_service.api.id
}

resource "google_compute_managed_ssl_certificate" "api" {
  project = var.project_id
  name    = "aixion-api-staging-certificate"

  managed {
    domains = [var.api_domain]
  }
}

resource "google_compute_ssl_policy" "api" {
  project         = var.project_id
  name            = "aixion-api-staging-tls-policy"
  profile         = "MODERN"
  min_tls_version = "TLS_1_2"
}

resource "google_compute_target_https_proxy" "api" {
  project          = var.project_id
  name             = "aixion-api-staging-https-proxy"
  url_map          = google_compute_url_map.api.id
  ssl_certificates = [google_compute_managed_ssl_certificate.api.id]
  ssl_policy       = google_compute_ssl_policy.api.id
}

resource "google_compute_global_forwarding_rule" "api_https" {
  project               = var.project_id
  name                  = "aixion-api-staging-https-forwarding-rule"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  network_tier          = "PREMIUM"
  ip_protocol           = "TCP"
  port_range            = "443"
  ip_address            = google_compute_global_address.api.id
  target                = google_compute_target_https_proxy.api.id
}

output "api_load_balancer_ip" {
  description = "Reserved global IP to publish as the Spaceship A record after review."
  value       = google_compute_global_address.api.address
}
