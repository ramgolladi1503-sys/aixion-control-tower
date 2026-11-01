from pathlib import Path

PRODUCTION_TERRAFORM = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "gcp"
    / "production"
    / "main.tf"
)
PRODUCTION_LOCKFILE = PRODUCTION_TERRAFORM.with_name(".terraform.lock.hcl")


def test_production_https_template_is_pinned_to_requested_host_and_region() -> None:
    source = PRODUCTION_TERRAFORM.read_text(encoding="utf-8")

    assert 'default     = "asia-south1"' in source
    assert 'condition     = var.cloud_run_region == "asia-south1"' in source
    assert 'default     = "api.aixionlab.com"' in source
    assert 'condition     = var.api_domain == "api.aixionlab.com"' in source
    assert 'min_tls_version = "TLS_1_2"' in source
    assert 'port_range            = "443"' in source
    assert 'protocol              = "HTTP"' in source


def test_production_resources_remain_explicitly_disabled_by_default() -> None:
    source = PRODUCTION_TERRAFORM.read_text(encoding="utf-8")

    assert 'variable "enable_production_load_balancer"' in source
    assert "default     = false" in source
    assert 'resource "terraform_data" "production_apply_guard"' in source
    assert "depends_on = [terraform_data.production_apply_guard]" in source


def test_production_root_requires_gcs_state_and_locks_provider() -> None:
    source = PRODUCTION_TERRAFORM.read_text(encoding="utf-8")
    lockfile = PRODUCTION_LOCKFILE.read_text(encoding="utf-8")

    assert 'backend "gcs" {}' in source
    assert 'version     = "7.46.1"' in lockfile
    assert "registry.terraform.io/hashicorp/google" in lockfile
