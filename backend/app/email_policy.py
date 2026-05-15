from __future__ import annotations

import socket
from collections.abc import Callable

LOCAL_OR_RESERVED_DOMAINS = {
    "localhost",
    "local",
    "invalid",
    "test",
}
LOCAL_OR_RESERVED_SUFFIXES = (
    ".localhost",
    ".local",
    ".invalid",
    ".test",
)

DomainResolver = Callable[[str], bool]


def validate_registration_email_deliverability(email: str, resolver: DomainResolver | None = None) -> str:
    normalized_email = email.lower().strip()
    if "@" not in normalized_email:
        raise ValueError("Email address is invalid")
    local_part, domain = normalized_email.rsplit("@", 1)
    if not local_part or not domain:
        raise ValueError("Email address is invalid")
    if domain in LOCAL_OR_RESERVED_DOMAINS or domain.endswith(LOCAL_OR_RESERVED_SUFFIXES):
        raise ValueError("Email domain must be publicly deliverable")
    if "." not in domain:
        raise ValueError("Email domain must include a public suffix")

    resolver = resolver or domain_has_dns_records
    if not resolver(domain):
        raise ValueError("Email domain does not appear to receive mail")
    return normalized_email


def domain_has_dns_records(domain: str) -> bool:
    try:
        socket.getaddrinfo(domain, 0, type=socket.SOCK_STREAM)
        return True
    except socket.gaierror:
        return False
