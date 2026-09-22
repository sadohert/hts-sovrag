"""Resolve a Mattermost user to a Principal from admin-managed user attributes (ADR 0010).

Three GET calls with the bot token, in order: the user (proves the ID exists, gives the
username), the attribute field definitions, and the user's values. Only admin-managed
select fields named sovrag_clearance and sovrag_departments count, and their option IDs are
mapped to names through the definitions fetched in the same resolution. A known user with
nothing recognised is LEAST_PRIVILEGED with a note saying why. Anything the resolver cannot
verify, a transport error, a non-200 status, an unknown or deactivated user, a reply of the
wrong shape, is a ResolutionError, never a downgrade (ADR 0009).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx2

from sovrag.auth.principal import ANONYMOUS_ID, LEAST_PRIVILEGED, MEMBERSHIPS, Clearance, Principal
from sovrag.config import Settings

CLEARANCE_FIELD = "sovrag_clearance"
DEPARTMENTS_FIELD = "sovrag_departments"
ADMIN_MANAGED = "admin"
_USER_ID = re.compile(r"^[a-z0-9]{26}$")


class ResolutionError(Exception):
    """Mattermost could not tell us what the user is entitled to."""


@dataclass(frozen=True)
class Resolution:
    user_id: str
    username: str
    principal: Principal
    attributes: Mapping[str, str | list[str]]
    note: str | None


class MattermostResolver:
    def __init__(
        self,
        base_url: str,
        bot_token: str,
        *,
        timeout_s: float = 5.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._client = httpx2.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=httpx2.Timeout(timeout_s, connect=5.0),
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> MattermostResolver:
        if not settings.mattermost_url:
            raise ValueError("MATTERMOST_URL is required in mattermost mode")
        if not settings.mattermost_bot_token:
            raise ValueError("MATTERMOST_BOT_TOKEN is required in mattermost mode")
        return cls(settings.mattermost_url, settings.mattermost_bot_token)

    def resolve(self, user_id: str) -> Resolution:
        if not _USER_ID.match(user_id):
            raise ResolutionError("malformed user id")
        user = self._get(f"/api/v4/users/{user_id}", unknown_on_404=True)
        if not isinstance(user, dict) or not isinstance(user.get("username"), str):
            raise ResolutionError("user reply has the wrong shape")
        if user.get("delete_at"):
            raise ResolutionError(f"user {user_id} is deactivated")
        username: str = user["username"]
        fields = self._get("/api/v4/custom_profile_attributes/fields")
        if not isinstance(fields, list):
            raise ResolutionError("fields reply has the wrong shape")
        values = self._get(f"/api/v4/users/{user_id}/custom_profile_attributes")
        if not isinstance(values, dict):
            raise ResolutionError("values reply has the wrong shape")
        return _bind(user_id, username, fields, values)

    def _get(self, path: str, *, unknown_on_404: bool = False) -> Any:
        try:
            response = self._client.get(path)
        except httpx2.HTTPError as exc:
            raise ResolutionError(f"{path}: {type(exc).__name__}") from None
        if response.status_code == 404 and unknown_on_404:
            raise ResolutionError("unknown user")
        if response.status_code != 200:
            raise ResolutionError(f"{path}: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise ResolutionError(f"{path}: reply is not JSON") from None


def _bind(user_id: str, username: str, fields: list[Any], values: Mapping[str, Any]) -> Resolution:
    notes: list[str] = []
    snapshot: dict[str, str | list[str]] = {}

    clearance: Clearance | None = None
    field = _admin_managed(fields, CLEARANCE_FIELD, "select", notes)
    if field is not None:
        raw = values.get(field["id"])
        if raw is None:
            notes.append(f"no {CLEARANCE_FIELD} value")
        elif not isinstance(raw, str):
            notes.append(f"{CLEARANCE_FIELD} value has the wrong shape")
        else:
            name = _option_name(field, raw)
            if name is None:
                notes.append(f"unrecognised {CLEARANCE_FIELD} option")
            elif name not in Clearance.__members__:
                notes.append(f"{CLEARANCE_FIELD} {name!r} is outside the vocabulary")
            else:
                clearance = Clearance(name)
                snapshot[CLEARANCE_FIELD] = name

    departments: set[str] = set()
    field = _admin_managed(fields, DEPARTMENTS_FIELD, "multiselect", notes)
    if field is not None:
        raw = values.get(field["id"])
        if raw is None:
            notes.append(f"no {DEPARTMENTS_FIELD} value")
        elif not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
            notes.append(f"{DEPARTMENTS_FIELD} value has the wrong shape")
        else:
            for option_id in raw:
                name = _option_name(field, option_id)
                if name is None:
                    notes.append(f"unrecognised {DEPARTMENTS_FIELD} option")
                elif name not in MEMBERSHIPS:
                    notes.append(f"{DEPARTMENTS_FIELD} {name!r} is outside the vocabulary")
                else:
                    departments.add(name)
            if departments:
                snapshot[DEPARTMENTS_FIELD] = sorted(departments)

    if clearance is None:
        notes.append("bound to least privilege")
        return Resolution(user_id, username, LEAST_PRIVILEGED, snapshot, "; ".join(notes))
    if username.lower() == ANONYMOUS_ID:
        raise ResolutionError(f"username {username!r} is reserved and cannot be entitled")
    principal = Principal(id=username, clearance=clearance, departments=frozenset(departments))
    return Resolution(user_id, username, principal, snapshot, "; ".join(notes) or None)


def _admin_managed(
    fields: list[Any], name: str, expected_type: str, notes: list[str]
) -> dict[str, Any] | None:
    for field in fields:
        if not isinstance(field, dict) or field.get("name") != name:
            continue
        attrs = field.get("attrs")
        if not isinstance(attrs, dict) or attrs.get("managed") != ADMIN_MANAGED:
            notes.append(f"{name} is not admin-managed")
            return None
        if field.get("type") != expected_type or not isinstance(field.get("id"), str):
            notes.append(f"{name} is not a {expected_type} field")
            return None
        return field
    notes.append(f"no {name} field")
    return None


def _option_name(field: Mapping[str, Any], option_id: str) -> str | None:
    options = field.get("attrs", {}).get("options")
    if not isinstance(options, list):
        return None
    for option in options:
        if isinstance(option, dict) and option.get("id") == option_id:
            name = option.get("name")
            return name if isinstance(name, str) else None
    return None
