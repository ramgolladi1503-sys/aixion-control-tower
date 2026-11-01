"""Recompute planning-only Cloud Run scenarios from their Markdown inputs.

These checks validate worksheet arithmetic, not regional prices, a quote, a
project budget, or authorization to provision resources.
"""
from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pytest


SCENARIOS = Path(__file__).resolve().parents[2] / "docs" / "STAGING_COST_SCENARIOS.md"
CENT = Decimal("0.01")


def _decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", ""))


def _quantity(value: str) -> Decimal:
    normalized = value.strip().lower()
    if normalized.endswith(" million"):
        return _decimal(normalized.removesuffix(" million")) * Decimal(1_000_000)
    return _decimal(normalized)


def _count(value: str) -> Decimal:
    return Decimal(1) if value.strip().lower() == "one" else _decimal(value)


def _one(pattern: str, text: str, label: str) -> tuple[str, ...]:
    match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    assert match is not None, f"worksheet is missing a parseable {label} input"
    return match.groups()


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _read_inputs(text: str) -> dict[str, object]:
    prose = " ".join(text.split())
    period_days, = _one(r"\*\*Currency and period:\*\* USD,\s*(\d+)-day month", prose, "month length")
    api_cpu_rate, api_memory_rate, api_request_rate = _one(
        r"request-based service rates of `\$([0-9.]+)` per vCPU-second, `\$([0-9.]+)` per GiB-second, and `\$([0-9.]+)` per million requests",
        prose,
        "API rates",
    )
    api_cpu_free, api_memory_free, api_requests_free = _one(
        r"request-based free allowance (?:is|of) ([\d,]+) vCPU-seconds, ([\d,]+) GiB-seconds, and ([\d,]+(?: million)?) requests per month",
        prose,
        "API free allowance",
    )
    jobs_cpu_rate, jobs_memory_rate = _one(
        r"instance-based rates of `\$([0-9.]+)` per vCPU-second and `\$([0-9.]+)` per GiB-second",
        prose,
        "Jobs rates",
    )
    jobs_cpu_free, jobs_memory_free = _one(
        r"with ([\d,]+) vCPU-seconds and ([\d,]+) GiB-seconds of monthly free allowance",
        prose,
        "Jobs free allowance",
    )
    api_vcpus, api_gib = _one(
        r"API service: request-based billing,\s*(one|[0-9.]+) vCPU,\s*([0-9.]+) GiB memory",
        prose,
        "API sizing",
    )
    jobs_count, jobs_vcpus, jobs_gib, max_job_seconds = _one(
        r"Two independent (\w+)-task Jobs.*?Each Job uses (one|[0-9.]+) vCPU and ([0-9.]+) GiB; actual run time is below or equal to ([0-9.]+) seconds",
        prose,
        "Jobs count, sizing, and maximum billed duration",
    )
    # The checked-in worksheet says one-task Jobs. Keep this explicit so a
    # topology edit cannot silently change the modeled invocation count.
    assert jobs_count.lower() == "one"
    minimum_seconds, = _one(
        r"Cloud Run Jobs have a (one|[0-9]+)-minute minimum billed instance duration",
        prose,
        "Jobs minimum duration",
    )
    rows: dict[str, dict[str, object]] = {}
    table = re.search(
        r"\| Scenario \| API workload \| Two Job cadence and run time \| Cloud Run subtotal if free tiers are available \| Cloud Run subtotal with free tiers exhausted \|\n\|[-| :]+\|\n(?P<rows>(?:\|[^\n]+\n)+)",
        text,
        re.MULTILINE,
    )
    assert table is not None, "worksheet is missing its structured scenario table"
    for line in table.group("rows").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        assert len(cells) == 5, f"unexpected scenario row: {line}"
        name, api_workload, job_workload, free_total, no_free_total = cells
        api_requests, api_seconds = _one(
            r"([\d,]+) requests/month;\s*([0-9.]+) sec average", api_workload, f"{name} API workload"
        )
        cadence_minutes, job_actual_seconds = _one(
            r"Every ([\d,]+) minute(?:s)?;\s*([0-9.]+) sec actual per Job", job_workload, f"{name} Job cadence"
        )
        rows[name.lower()] = {
            "requests": _decimal(api_requests),
            "api_seconds": _decimal(api_seconds),
            "cadence_minutes": _decimal(cadence_minutes),
            "job_actual_seconds": _decimal(job_actual_seconds),
            "free_total": _decimal(re.sub(r"[^0-9.]", "", free_total)),
            "no_free_total": _decimal(re.sub(r"[^0-9.]", "", no_free_total)),
        }
    return {
        "days": int(period_days),
        "api_cpu_rate": _decimal(api_cpu_rate),
        "api_memory_rate": _decimal(api_memory_rate),
        "api_request_rate": _decimal(api_request_rate),
        "api_cpu_free": _decimal(api_cpu_free),
        "api_memory_free": _decimal(api_memory_free),
        "api_requests_free": _quantity(api_requests_free),
        "jobs_cpu_rate": _decimal(jobs_cpu_rate),
        "jobs_memory_rate": _decimal(jobs_memory_rate),
        "jobs_cpu_free": _decimal(jobs_cpu_free),
        "jobs_memory_free": _decimal(jobs_memory_free),
        "api_vcpus": _count(api_vcpus),
        "api_gib": _decimal(api_gib),
        "jobs_per_cadence": Decimal(2),
        "jobs_vcpus": _count(jobs_vcpus),
        "jobs_gib": _decimal(jobs_gib),
        "jobs_max_seconds": _decimal(max_job_seconds),
        "jobs_min_seconds": _count(minimum_seconds) * Decimal(60),
        "rows": rows,
    }


def _scenario_cost(inputs: dict[str, object], row: dict[str, object], *, free_tier: bool) -> Decimal:
    requests = row["requests"]
    api_seconds = requests * row["api_seconds"]
    api_cpu_seconds = api_seconds * inputs["api_vcpus"]
    api_memory_seconds = api_seconds * inputs["api_gib"]

    # Apply the free allowance to aggregate monthly usage for the modeled
    # billing account, independently for CPU, memory, and request count.
    api_cpu_billable = max(api_cpu_seconds - (inputs["api_cpu_free"] if free_tier else 0), 0)
    api_memory_billable = max(api_memory_seconds - (inputs["api_memory_free"] if free_tier else 0), 0)
    api_requests_billable = max(requests - (inputs["api_requests_free"] if free_tier else 0), 0)
    api_cost = (
        api_cpu_billable * inputs["api_cpu_rate"]
        + api_memory_billable * inputs["api_memory_rate"]
        + api_requests_billable / Decimal(1_000_000) * inputs["api_request_rate"]
    )

    invocations = (
        inputs["days"]
        * Decimal(24 * 60)
        / row["cadence_minutes"]
        * inputs["jobs_per_cadence"]
    )
    billed_seconds = max(row["job_actual_seconds"], inputs["jobs_min_seconds"])
    billed_seconds = min(billed_seconds, inputs["jobs_max_seconds"])
    jobs_cpu_seconds = invocations * billed_seconds * inputs["jobs_vcpus"]
    jobs_memory_seconds = invocations * billed_seconds * inputs["jobs_gib"]
    jobs_cpu_billable = max(jobs_cpu_seconds - (inputs["jobs_cpu_free"] if free_tier else 0), 0)
    jobs_memory_billable = max(jobs_memory_seconds - (inputs["jobs_memory_free"] if free_tier else 0), 0)
    jobs_cost = (
        jobs_cpu_billable * inputs["jobs_cpu_rate"]
        + jobs_memory_billable * inputs["jobs_memory_rate"]
    )
    return _money(api_cost + jobs_cost)


@pytest.fixture(scope="module")
def worksheet() -> dict[str, object]:
    text = SCENARIOS.read_text(encoding="utf-8")
    assert "Planning worksheet only; not a quote" in text
    return _read_inputs(text)


@pytest.mark.parametrize("scenario", ["low", "expected", "high"])
def test_cloud_run_scenario_totals_recompute_from_documented_inputs(
    worksheet: dict[str, object], scenario: str
) -> None:
    row = worksheet["rows"][scenario]
    assert _scenario_cost(worksheet, row, free_tier=True) == row["free_total"]
    assert _scenario_cost(worksheet, row, free_tier=False) == row["no_free_total"]


def test_jobs_minimum_billing_duration_is_applied(worksheet: dict[str, object]) -> None:
    low = worksheet["rows"]["low"]
    assert low["job_actual_seconds"] == Decimal(10)
    assert worksheet["jobs_min_seconds"] == Decimal(60)
    # Jobs alone exceed the free CPU allowance only after the 60-second minimum.
    invocations = worksheet["days"] * Decimal(24 * 60) / low["cadence_minutes"] * worksheet["jobs_per_cadence"]
    with_minimum = invocations * worksheet["jobs_min_seconds"] * worksheet["jobs_vcpus"]
    without_minimum = invocations * low["job_actual_seconds"] * worksheet["jobs_vcpus"]
    assert with_minimum > worksheet["jobs_cpu_free"]
    assert without_minimum < worksheet["jobs_cpu_free"]
