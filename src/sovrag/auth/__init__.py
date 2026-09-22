"""Identity and the entitlement vocabulary (principal.py) and the Mattermost resolver
(mattermost.py)."""

from sovrag.auth.mattermost import MattermostResolver, Resolution, ResolutionError
from sovrag.auth.principal import (
    ANONYMOUS_ID,
    CLASSIFICATION_RANK,
    DEFAULT_PRINCIPALS_PATH,
    DEPARTMENTS,
    GC_WIDE,
    LEAST_PRIVILEGED,
    MEMBERSHIPS,
    Clearance,
    Principal,
    load_principals,
    resolve_principal,
)

__all__ = [
    "ANONYMOUS_ID",
    "CLASSIFICATION_RANK",
    "DEFAULT_PRINCIPALS_PATH",
    "DEPARTMENTS",
    "GC_WIDE",
    "LEAST_PRIVILEGED",
    "MEMBERSHIPS",
    "Clearance",
    "MattermostResolver",
    "Principal",
    "Resolution",
    "ResolutionError",
    "load_principals",
    "resolve_principal",
]
