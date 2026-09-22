"""The principal model: clearance levels, departments, and the shared label vocabulary."""

import dataclasses
from pathlib import Path

import pytest

from sovrag import auth
from sovrag.auth import (
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
from sovrag.corpus import manifest


def test_clearance_ranks_order_the_three_labels() -> None:
    assert Clearance.UNCLASSIFIED.rank < Clearance.PROTECTED_A.rank < Clearance.PROTECTED_B.rank


def test_rank_table_covers_exactly_the_clearance_labels() -> None:
    assert set(CLASSIFICATION_RANK) == {c.value for c in Clearance}


def test_clearance_is_built_from_its_label() -> None:
    assert Clearance("PROTECTED_A") is Clearance.PROTECTED_A
    with pytest.raises(ValueError):
        Clearance("SECRET")


def test_every_doc_type_classification_is_a_clearance_label() -> None:
    for label in manifest.CLASSIFICATION_BY_DOC_TYPE.values():
        assert Clearance(label).value == label


def test_manifest_uses_the_auth_department_vocabulary() -> None:
    assert manifest.DEPARTMENTS is auth.DEPARTMENTS
    assert GC_WIDE in DEPARTMENTS


def test_manifest_no_longer_defines_its_own_rank_table() -> None:
    assert not hasattr(manifest, "CLASSIFICATION_RANK")


def test_principal_is_frozen_and_hashable() -> None:
    p = Principal(id="analyst_dnd", clearance=Clearance.PROTECTED_A, departments=frozenset({"DND"}))
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.id = "someone_else"  # type: ignore[misc]
    assert p in {p}


def test_least_privileged_sees_the_minimum() -> None:
    assert LEAST_PRIVILEGED.id == ANONYMOUS_ID == "anonymous"
    assert LEAST_PRIVILEGED.clearance is Clearance.UNCLASSIFIED
    assert LEAST_PRIVILEGED.departments == frozenset()


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "principals.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_load_principals_builds_principals_from_yaml(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "principals:\n"
        "  reader:\n"
        "    clearance: UNCLASSIFIED\n"
        "    departments: []\n"
        "  analyst:\n"
        "    clearance: PROTECTED_A\n"
        "    departments: [DND, PS]\n",
    )
    assert load_principals(path) == {
        "reader": Principal("reader", Clearance.UNCLASSIFIED, frozenset()),
        "analyst": Principal("analyst", Clearance.PROTECTED_A, frozenset({"DND", "PS"})),
    }


def test_departments_key_may_be_omitted(tmp_path: Path) -> None:
    path = _write(tmp_path, "principals:\n  reader:\n    clearance: UNCLASSIFIED\n")
    assert load_principals(path)["reader"].departments == frozenset()


def test_unknown_clearance_is_rejected_and_names_the_principal(tmp_path: Path) -> None:
    path = _write(tmp_path, "principals:\n  spy:\n    clearance: SECRET\n")
    with pytest.raises(ValueError, match=r"spy: unknown clearance 'SECRET'"):
        load_principals(path)


def test_unknown_department_is_rejected_and_names_the_principal(tmp_path: Path) -> None:
    path = _write(
        tmp_path, "principals:\n  a:\n    clearance: PROTECTED_A\n    departments: [DND, NASA]\n"
    )
    with pytest.raises(ValueError, match=r"a: unknown departments \['NASA'\]"):
        load_principals(path)


def test_gc_wide_is_a_document_label_not_a_membership(tmp_path: Path) -> None:
    path = _write(
        tmp_path, "principals:\n  a:\n    clearance: PROTECTED_A\n    departments: [GC_WIDE]\n"
    )
    with pytest.raises(ValueError, match=r"a: unknown departments \['GC_WIDE'\]"):
        load_principals(path)


def test_the_anonymous_id_is_reserved(tmp_path: Path) -> None:
    path = _write(tmp_path, "principals:\n  anonymous:\n    clearance: PROTECTED_B\n")
    with pytest.raises(ValueError, match="reserved"):
        load_principals(path)


def test_resolve_principal_returns_the_named_principal(tmp_path: Path) -> None:
    path = _write(
        tmp_path, "principals:\n  a:\n    clearance: PROTECTED_B\n    departments: [TBS]\n"
    )
    assert resolve_principal("a", path) == Principal("a", Clearance.PROTECTED_B, frozenset({"TBS"}))


def test_resolve_principal_raises_keyerror_for_an_unknown_id(tmp_path: Path) -> None:
    path = _write(tmp_path, "principals:\n  a:\n    clearance: UNCLASSIFIED\n")
    with pytest.raises(KeyError, match="nobody"):
        resolve_principal("nobody", path)


def test_shipped_principals_cover_the_entitlement_grid() -> None:
    assert load_principals(DEFAULT_PRINCIPALS_PATH) == {
        "public": Principal("public", Clearance.UNCLASSIFIED, frozenset()),
        "analyst_dnd": Principal("analyst_dnd", Clearance.PROTECTED_A, frozenset({"DND"})),
        "analyst_ps": Principal("analyst_ps", Clearance.PROTECTED_A, frozenset({"PS"})),
        "cio_ssc": Principal("cio_ssc", Clearance.PROTECTED_A, frozenset({"SSC", "TBS"})),
        "auditor": Principal("auditor", Clearance.PROTECTED_B, MEMBERSHIPS),
    }


def test_the_auditor_holds_every_membership_so_it_really_sees_everything() -> None:
    auditor = load_principals(DEFAULT_PRINCIPALS_PATH)["auditor"]
    assert auditor.clearance is Clearance.PROTECTED_B
    assert auditor.departments == MEMBERSHIPS


# The vocabulary and the rank table.


def test_memberships_are_the_departments_without_the_document_only_label() -> None:
    assert DEPARTMENTS - {GC_WIDE} == MEMBERSHIPS
    assert "CSE" in MEMBERSHIPS


def test_rank_follows_the_enum_order() -> None:
    assert [c.rank for c in Clearance] == [0, 1, 2]


def test_the_rank_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        CLASSIFICATION_RANK["PROTECTED_B"] = 0  # type: ignore[index]


# The type validates itself, so every source of principals gets the same guarantees.


def test_a_plain_string_is_not_a_clearance() -> None:
    with pytest.raises(ValueError, match="u: clearance must be a Clearance"):
        Principal("u", "PROTECTED_B", frozenset())  # type: ignore[arg-type]


def test_a_principal_cannot_hold_gc_wide_or_an_unknown_membership() -> None:
    with pytest.raises(ValueError, match=r"u: unknown departments \['GC_WIDE'\]"):
        Principal("u", Clearance.PROTECTED_A, frozenset({GC_WIDE}))
    with pytest.raises(ValueError, match=r"u: unknown departments \['NASA'\]"):
        Principal("u", Clearance.PROTECTED_A, frozenset({"DND", "NASA"}))


def test_departments_must_be_a_frozenset() -> None:
    with pytest.raises(ValueError, match="u: departments must be a frozenset"):
        Principal("u", Clearance.PROTECTED_A, {"DND"})  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_id", ["", 123, False, None])
def test_the_id_must_be_a_non_empty_string(bad_id: object) -> None:
    with pytest.raises(ValueError, match="id must be a non-empty string"):
        Principal(bad_id, Clearance.UNCLASSIFIED, frozenset())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("principal_id", "clearance", "departments"),
    [
        ("anonymous", Clearance.PROTECTED_B, frozenset()),
        ("anonymous", Clearance.UNCLASSIFIED, frozenset({"DND"})),
        ("Anonymous", Clearance.UNCLASSIFIED, frozenset()),
        ("ANONYMOUS", Clearance.PROTECTED_A, frozenset()),
    ],
)
def test_anonymous_in_any_case_exists_only_as_the_least_privileged_principal(
    principal_id: str, clearance: Clearance, departments: frozenset[str]
) -> None:
    with pytest.raises(ValueError, match="reserved"):
        Principal(principal_id, clearance, departments)


# The file loads strictly: every malformed input is a ValueError that names the principal.


def test_a_repeated_id_is_rejected_instead_of_the_last_entry_winning(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "principals:\n"
        "  a:\n"
        "    clearance: UNCLASSIFIED\n"
        "  a:\n"
        "    clearance: PROTECTED_B\n"
        "    departments: [DND]\n",
    )
    with pytest.raises(ValueError, match=r"duplicate key 'a' at line 4"):
        load_principals(path)


def test_a_repeated_field_inside_an_entry_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path, "principals:\n  a:\n    clearance: UNCLASSIFIED\n    clearance: PROTECTED_B\n"
    )
    with pytest.raises(ValueError, match="duplicate key 'clearance'"):
        load_principals(path)


@pytest.mark.parametrize("key", ["123", "no", "1.5"])
def test_an_id_that_yaml_does_not_read_as_a_string_is_rejected(tmp_path: Path, key: str) -> None:
    path = _write(tmp_path, f"principals:\n  {key}:\n    clearance: UNCLASSIFIED\n")
    with pytest.raises(ValueError, match="must be a string"):
        load_principals(path)


def test_a_case_variant_of_the_reserved_id_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "principals:\n  Anonymous:\n    clearance: PROTECTED_B\n")
    with pytest.raises(ValueError, match="reserved"):
        load_principals(path)


@pytest.mark.parametrize("body", ["", "other: {}\n", "principals: []\n", "- a\n- b\n"])
def test_a_file_without_a_principals_mapping_is_rejected(tmp_path: Path, body: str) -> None:
    path = _write(tmp_path, body)
    with pytest.raises(ValueError, match="top-level 'principals' mapping"):
        load_principals(path)


def test_an_entry_with_no_body_is_rejected_and_named(tmp_path: Path) -> None:
    path = _write(tmp_path, "principals:\n  a:\n")
    with pytest.raises(ValueError, match="a: entry must be a mapping"):
        load_principals(path)


def test_a_missing_clearance_is_rejected_and_named(tmp_path: Path) -> None:
    path = _write(tmp_path, "principals:\n  bad:\n    departments: [DND]\n")
    with pytest.raises(ValueError, match="bad: missing clearance"):
        load_principals(path)


@pytest.mark.parametrize("departments", ["DND", "[1, X]", "{DND: true}", "0"])
def test_departments_must_be_a_list_of_strings(tmp_path: Path, departments: str) -> None:
    path = _write(
        tmp_path, f"principals:\n  a:\n    clearance: PROTECTED_A\n    departments: {departments}\n"
    )
    with pytest.raises(ValueError, match="a: departments must be a list of strings"):
        load_principals(path)


def test_one_malformed_entry_is_a_valueerror_not_an_unknown_id(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "principals:\n  good:\n    clearance: UNCLASSIFIED\n  bad:\n    departments: [DND]\n",
    )
    with pytest.raises(ValueError, match="bad: missing clearance"):
        resolve_principal("good", path)


@pytest.mark.parametrize(
    "body",
    [
        "principals:\n  a:\n    clearance: [UNCLASSIFIED\n",  # unterminated flow sequence
        "principals:\n\t a:\n    clearance: UNCLASSIFIED\n",  # tab indentation
        "principals:\n  ? [a, b]\n  : {clearance: UNCLASSIFIED}\n",  # unhashable key
    ],
)
def test_a_file_yaml_cannot_parse_is_a_valueerror_naming_the_path(
    tmp_path: Path, body: str
) -> None:
    path = _write(tmp_path, body)
    with pytest.raises(ValueError, match=rf"{path.name}: not a valid principals file"):
        load_principals(path)
