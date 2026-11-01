from pathlib import Path

PRODUCTION_TERRAFORM = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "gcp"
    / "production"
    / "main.tf"
)


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
