"""Read-only Google credentials. Phase 1 refuses any scope that could write."""

from __future__ import annotations

import os

GSC_READONLY = "https://www.googleapis.com/auth/webmasters.readonly"
GA4_READONLY = "https://www.googleapis.com/auth/analytics.readonly"
READONLY_SCOPES = (GSC_READONLY, GA4_READONLY)


class CredentialsError(RuntimeError):
    pass


def assert_readonly(scopes: tuple[str, ...] | list[str]) -> None:
    bad = [s for s in scopes if not s.endswith(".readonly")]
    if bad:
        raise CredentialsError(f"Phase 1 is read-only; refusing write-capable scopes: {bad}")


def get_credentials(scopes: tuple[str, ...] = READONLY_SCOPES):
    """Return (credentials, identity) using Application Default Credentials.

    Uses GOOGLE_APPLICATION_CREDENTIALS (a service-account key outside the repo).
    Never logs or returns key material; `identity` is only the service-account email.
    """
    assert_readonly(scopes)

    key_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not key_path:
        raise CredentialsError(
            "GOOGLE_APPLICATION_CREDENTIALS is not set. Point it at the service-account key "
            "(stored outside this repo)."
        )
    if not os.path.isfile(key_path):
        raise CredentialsError("GOOGLE_APPLICATION_CREDENTIALS points to a file that does not exist.")

    import google.auth  # imported late so offline tests don't need the package

    credentials, _project = google.auth.default(scopes=list(scopes))
    identity = getattr(credentials, "service_account_email", None) or "unknown"
    return credentials, identity
