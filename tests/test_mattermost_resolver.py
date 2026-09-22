"""Mattermost attribute resolver: recorded responses in, Principal out, errors on doubt."""

from __future__ import annotations

import json

import httpx2 as httpx
import pytest
from sovrag.auth.mattermost import CLEARANCE_FIELD, DEPARTMENTS_FIELD

from sovrag.auth import (
    LEAST_PRIVILEGED,
    Clearance,
    MattermostResolver,
    Resolution,
    ResolutionError,
)
from sovrag.config import Settings

# Mattermost IDs are 26 lowercase base-32 characters. These are shaped like real ones.
UID_DND = "u1dnd0000000000000000000000"[:26]
UID_AUD = "u2aud0000000000000000000000"[:26]
UID_PUB = "u3pub0000000000000000000000"[:26]
UID_GONE = "u4gone000000000000000000000"[:26]
F_CLR = "f1clr0000000000000000000000"[:26]
F_DEP = "f2dep0000000000000000000000"[:26]
F_TXT = "f3txt0000000000000000000000"[:26]
OPT = {
    name: f"o{name.lower()}".ljust(26, "0")
    for name in (
        "UNCLASSIFIED",
        "PROTECTED_A",
        "PROTECTED_B",
        "DND",
        "CSE",
        "PS",
        "PSPC",
        "SSC",
        "TBS",
    )
}


def _field(fid: str, name: str, ftype: str, options: list[str], managed: str = "admin") -> dict:
    return {
        "id": fid,
        "group_id": "g1",
        "name": name,
        "type": ftype,
        "attrs": {
            "visibility": "when_set",
            "sort_order": 0,
            "options": [{"id": OPT[o], "name": o, "color": ""} for o in options],
            "value_type": "",
            "ldap": "",
            "saml": "",
            "managed": managed,
        },
        "target_id": "",
        "target_type": "",
        "object_type": "user",
        "create_at": 1,
        "update_at": 1,
        "delete_at": 0,
    }


FIELDS = [
    _field(F_CLR, CLEARANCE_FIELD, "select", ["UNCLASSIFIED", "PROTECTED_A", "PROTECTED_B"]),
    _field(F_DEP, DEPARTMENTS_FIELD, "multiselect", ["DND", "CSE", "PS", "PSPC", "SSC", "TBS"]),
    {**_field(F_TXT, "title", "text", []), "attrs": {"managed": "", "options": []}},
]

USERS = {
    UID_DND: {"id": UID_DND, "username": "analyst_dnd", "delete_at": 0},
    UID_AUD: {"id": UID_AUD, "username": "auditor", "delete_at": 0},
    UID_PUB: {"id": UID_PUB, "username": "public", "delete_at": 0},
}

VALUES = {
    UID_DND: {F_CLR: OPT["PROTECTED_A"], F_DEP: [OPT["DND"]], F_TXT: "Analyst"},
    UID_AUD: {
        F_CLR: OPT["PROTECTED_B"],
        F_DEP: [OPT[d] for d in ("CSE", "DND", "PS", "PSPC", "SSC", "TBS")],
    },
    UID_PUB: {},
}


class FakeMattermost:
    """Routes the three GET paths; records every request so tests can assert the calls."""

    def __init__(
        self,
        *,
        fields: list[dict] | None = None,
        values: dict[str, dict] | None = None,
        users: dict[str, dict] | None = None,
        fail: dict[str, int | Exception] | None = None,
    ) -> None:
        self.fields = FIELDS if fields is None else fields
        self.values = VALUES if values is None else values
        self.users = USERS if users is None else users
        self.fail = fail or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        for key, outcome in self.fail.items():
            if key in path:
                if isinstance(outcome, Exception):
                    raise outcome
                return httpx.Response(outcome, json={"message": "nope"})
        if path == "/api/v4/custom_profile_attributes/fields":
            return httpx.Response(200, json=self.fields)
        if path.startswith("/api/v4/users/") and path.endswith("/custom_profile_attributes"):
            uid = path.split("/")[4]
            return httpx.Response(200, json=self.values.get(uid, {}))
        if path.startswith("/api/v4/users/"):
            uid = path.split("/")[4]
            if uid in self.users:
                return httpx.Response(200, json=self.users[uid])
            return httpx.Response(404, json={"id": "api.user.get.missing.app_error"})
        return httpx.Response(500, json={"message": f"unexpected {path}"})


def _resolver(server: FakeMattermost) -> MattermostResolver:
    return MattermostResolver("http://mm.test", "bot-token", transport=httpx.MockTransport(server))


def test_an_entitled_user_becomes_a_principal_named_by_username() -> None:
    server = FakeMattermost()
    result = _resolver(server).resolve(UID_DND)
    assert isinstance(result, Resolution)
    assert result.principal.id == "analyst_dnd"
    assert result.principal.clearance is Clearance.PROTECTED_A
    assert result.principal.departments == frozenset({"DND"})
    assert result.attributes == {CLEARANCE_FIELD: "PROTECTED_A", DEPARTMENTS_FIELD: ["DND"]}
    assert result.note is None
    assert result.user_id == UID_DND and result.username == "analyst_dnd"


def test_the_auditor_holds_every_membership() -> None:
    result = _resolver(FakeMattermost()).resolve(UID_AUD)
    assert result.principal.clearance is Clearance.PROTECTED_B
    assert result.principal.departments == frozenset({"CSE", "DND", "PS", "PSPC", "SSC", "TBS"})
    assert result.attributes[DEPARTMENTS_FIELD] == ["CSE", "DND", "PS", "PSPC", "SSC", "TBS"]


def test_three_calls_in_order_with_the_bot_token_and_nothing_else() -> None:
    server = FakeMattermost()
    _resolver(server).resolve(UID_DND)
    assert [r.url.path for r in server.requests] == [
        f"/api/v4/users/{UID_DND}",
        "/api/v4/custom_profile_attributes/fields",
        f"/api/v4/users/{UID_DND}/custom_profile_attributes",
    ]
    assert all(r.method == "GET" for r in server.requests)
    assert all(r.headers["authorization"] == "Bearer bot-token" for r in server.requests)


def test_a_known_user_with_no_values_is_least_privileged_with_a_note() -> None:
    result = _resolver(FakeMattermost()).resolve(UID_PUB)
    assert result.principal == LEAST_PRIVILEGED
    assert result.attributes == {}
    assert result.note is not None and CLEARANCE_FIELD in result.note


def test_a_value_on_a_field_that_is_not_admin_managed_is_ignored() -> None:
    fields = [
        _field(
            F_CLR,
            CLEARANCE_FIELD,
            "select",
            ["UNCLASSIFIED", "PROTECTED_A", "PROTECTED_B"],
            managed="",
        ),
        FIELDS[1],
    ]
    result = _resolver(FakeMattermost(fields=fields)).resolve(UID_DND)
    assert result.principal == LEAST_PRIVILEGED
    assert CLEARANCE_FIELD not in result.attributes
    assert result.note is not None and "not admin-managed" in result.note


def test_a_clearance_with_no_departments_is_still_a_principal() -> None:
    values = {UID_DND: {F_CLR: OPT["PROTECTED_A"]}}
    result = _resolver(FakeMattermost(values=values)).resolve(UID_DND)
    assert result.principal.clearance is Clearance.PROTECTED_A
    assert result.principal.departments == frozenset()
    assert result.attributes == {CLEARANCE_FIELD: "PROTECTED_A"}


def test_an_option_id_the_definition_does_not_list_is_ignored() -> None:
    values = {UID_DND: {F_CLR: "zzzzzzzzzzzzzzzzzzzzzzzzzz", F_DEP: [OPT["DND"], "nope"]}}
    result = _resolver(FakeMattermost(values=values)).resolve(UID_DND)
    assert result.principal == LEAST_PRIVILEGED
    assert result.attributes == {DEPARTMENTS_FIELD: ["DND"]}
    assert result.note is not None and "unrecognised" in result.note


def test_an_option_name_outside_the_vocabulary_is_ignored() -> None:
    extra = "ozzz".ljust(26, "0")
    fields = [
        {
            **FIELDS[0],
            "attrs": {
                **FIELDS[0]["attrs"],
                "options": [*FIELDS[0]["attrs"]["options"], {"id": extra, "name": "SECRET"}],
            },
        },
        {
            **FIELDS[1],
            "attrs": {
                **FIELDS[1]["attrs"],
                "options": [*FIELDS[1]["attrs"]["options"], {"id": extra, "name": "GC_WIDE"}],
            },
        },
    ]
    values = {UID_DND: {F_CLR: extra, F_DEP: [extra, OPT["DND"]]}}
    result = _resolver(FakeMattermost(fields=fields, values=values)).resolve(UID_DND)
    assert result.principal == LEAST_PRIVILEGED
    assert result.attributes == {DEPARTMENTS_FIELD: ["DND"]}


def test_a_select_field_of_the_wrong_type_is_ignored() -> None:
    fields = [_field(F_CLR, CLEARANCE_FIELD, "text", []), FIELDS[1]]
    values = {UID_DND: {F_CLR: "PROTECTED_B", F_DEP: [OPT["DND"]]}}
    result = _resolver(FakeMattermost(fields=fields, values=values)).resolve(UID_DND)
    assert result.principal == LEAST_PRIVILEGED


def test_a_value_of_the_wrong_json_shape_is_ignored() -> None:
    values = {UID_DND: {F_CLR: [OPT["PROTECTED_A"]], F_DEP: OPT["DND"]}}
    result = _resolver(FakeMattermost(values=values)).resolve(UID_DND)
    assert result.principal == LEAST_PRIVILEGED
    assert result.attributes == {}


def test_an_unknown_user_is_an_error_not_a_downgrade() -> None:
    server = FakeMattermost()
    with pytest.raises(ResolutionError, match="unknown user"):
        _resolver(server).resolve(UID_GONE)
    assert len(server.requests) == 1


def test_a_deactivated_user_is_an_error() -> None:
    users = {**USERS, UID_DND: {**USERS[UID_DND], "delete_at": 1700000000000}}
    with pytest.raises(ResolutionError, match="deactivated"):
        _resolver(FakeMattermost(users=users)).resolve(UID_DND)


def test_a_user_named_anonymous_cannot_be_entitled() -> None:
    users = {**USERS, UID_DND: {**USERS[UID_DND], "username": "anonymous"}}
    with pytest.raises(ResolutionError, match="anonymous"):
        _resolver(FakeMattermost(users=users)).resolve(UID_DND)


@pytest.mark.parametrize(
    "path_part",
    ["/api/v4/users/", "custom_profile_attributes/fields", "/custom_profile_attributes"],
)
@pytest.mark.parametrize(
    "outcome",
    [500, 401, httpx.ConnectTimeout("slow"), httpx.ConnectError("down")],
    ids=["500", "401", "timeout", "refused"],
)
def test_any_failed_call_is_an_error_and_never_least_privilege(
    path_part: str, outcome: int | Exception
) -> None:
    key = "/api/v4/users/" if path_part == "/api/v4/users/" else path_part
    server = FakeMattermost(fail={key: outcome})
    with pytest.raises(ResolutionError):
        _resolver(server).resolve(UID_DND)


def test_a_reply_that_is_not_json_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>login</html>")

    resolver = MattermostResolver("http://mm.test", "t", transport=httpx.MockTransport(handler))
    with pytest.raises(ResolutionError, match="not JSON"):
        resolver.resolve(UID_DND)


def test_a_fields_reply_of_the_wrong_shape_is_an_error() -> None:
    server = FakeMattermost(fields={"not": "a list"})  # type: ignore[arg-type]
    with pytest.raises(ResolutionError, match="shape"):
        _resolver(server).resolve(UID_DND)


@pytest.mark.parametrize("bad", ["", "short", "../admin", "A" * 26, "x" * 27, "u1dnd/../"])
def test_a_malformed_user_id_never_reaches_the_network(bad: str) -> None:
    server = FakeMattermost()
    with pytest.raises(ResolutionError, match="malformed"):
        _resolver(server).resolve(bad)
    assert server.requests == []


def test_the_token_never_appears_in_an_error_or_resolution() -> None:
    server = FakeMattermost(fail={"fields": 500})
    resolver = MattermostResolver(
        "http://mm.test", "s3cret-token", transport=httpx.MockTransport(server)
    )
    with pytest.raises(ResolutionError) as info:
        resolver.resolve(UID_DND)
    assert "s3cret" not in str(info.value) and "s3cret" not in repr(info.value)
    ok = _resolver(FakeMattermost()).resolve(UID_DND)
    assert "bot-token" not in repr(ok)


def test_from_settings_requires_both_values() -> None:
    with pytest.raises(ValueError, match="MATTERMOST_URL"):
        MattermostResolver.from_settings(Settings(mattermost_url="", mattermost_bot_token="t"))
    with pytest.raises(ValueError, match="MATTERMOST_BOT_TOKEN"):
        MattermostResolver.from_settings(
            Settings(mattermost_url="http://mm.test", mattermost_bot_token="")
        )
    resolver = MattermostResolver.from_settings(
        Settings(mattermost_url="http://mm.test/", mattermost_bot_token="t")
    )
    assert isinstance(resolver, MattermostResolver)


def test_resolution_is_json_serialisable_for_the_audit_log() -> None:
    result = _resolver(FakeMattermost()).resolve(UID_AUD)
    line = json.dumps(
        {"principal": result.principal.id, "attributes": result.attributes, "note": result.note}
    )
    assert json.loads(line)["attributes"][CLEARANCE_FIELD] == "PROTECTED_B"
