"""Cross-tenant leakage gate (docs/specs/evaluation-judge.md section 3)."""

from ops_ai.evaluation.leakage import Leak, find_leaks, leakage_metric
from ops_ai.evaluation.metrics import MetricResult

FOREIGN_UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def test_find_leaks_returns_nothing_for_a_clean_payload() -> None:
    # Arrange
    payload = {"evidence": [{"ref": "payment:abc", "summary": "ok"}], "n": 3}

    # Act
    leaks = find_leaks(payload, frozenset({"mch_foreign"}))

    # Assert
    assert leaks == ()


def test_find_leaks_returns_nothing_when_the_foreign_set_is_empty() -> None:
    # Arrange
    payload = {"summary": "mch_foreign is here"}

    # Act
    leaks = find_leaks(payload, frozenset())

    # Assert
    assert leaks == ()


def test_find_leaks_reports_the_path_of_a_leaking_list_item_value() -> None:
    # Arrange
    payload = {"evidence": [{"ref": "payment:ok"}, {"ref": "payment:mch_foreign"}]}

    # Act
    leaks = find_leaks(payload, frozenset({"mch_foreign"}))

    # Assert
    assert leaks == (Leak(path="$.evidence[1].ref", token="mch_foreign"),)


def test_find_leaks_reports_a_leaking_dict_key() -> None:
    # Arrange
    payload = {"mch_foreign": 1}

    # Act
    leaks = find_leaks(payload, frozenset({"mch_foreign"}))

    # Assert
    assert leaks == (Leak(path="$.mch_foreign", token="mch_foreign"),)


def test_find_leaks_reports_a_leaking_key_of_a_nested_dict_under_the_dotted_path() -> None:
    # Arrange
    payload = {"window": {"mch_foreign": 1}}

    # Act
    leaks = find_leaks(payload, frozenset({"mch_foreign"}))

    # Assert
    assert leaks == (Leak(path="$.window.mch_foreign", token="mch_foreign"),)


def test_find_leaks_reports_a_leaking_root_string_at_the_root_path() -> None:
    # Arrange / Act
    leaks = find_leaks("see mch_foreign", frozenset({"mch_foreign"}))

    # Assert
    assert leaks == (Leak(path="$", token="mch_foreign"),)


def test_find_leaks_reports_a_leaking_root_list_item_by_index() -> None:
    # Arrange / Act
    leaks = find_leaks(["fine", "mch_foreign"], frozenset({"mch_foreign"}))

    # Assert
    assert leaks == (Leak(path="$[1]", token="mch_foreign"),)


def test_find_leaks_matches_the_lowercase_form_of_a_token() -> None:
    # Arrange
    payload = {"x": "Seen MCH_FOREIGN today"}

    # Act
    leaks = find_leaks(payload, frozenset({"mch_foreign"}))

    # Assert
    assert [leak.path for leak in leaks] == ["$.x"]
    assert [leak.token.lower() for leak in leaks] == ["mch_foreign"]


def test_find_leaks_matches_a_token_that_is_in_the_foreign_set_as_written() -> None:
    # Arrange
    payload = {"x": "Seen MCH_FOREIGN today"}

    # Act
    leaks = find_leaks(payload, frozenset({"MCH_FOREIGN"}))

    # Assert
    assert leaks == (Leak(path="$.x", token="MCH_FOREIGN"),)


def test_find_leaks_splits_strings_on_characters_outside_letters_digits_underscore_and_hyphen() -> None:
    # Arrange
    payload = {"x": "ref=mch_foreign;(other)", "y": "payment:mch_foreign,"}

    # Act
    leaks = find_leaks(payload, frozenset({"mch_foreign"}))

    # Assert
    assert leaks == (
        Leak(path="$.x", token="mch_foreign"),
        Leak(path="$.y", token="mch_foreign"),
    )


def test_find_leaks_keeps_a_hyphenated_uuid_as_one_token() -> None:
    # Arrange
    payload = {"ref": f"payment:{FOREIGN_UUID}"}

    # Act
    leaks = find_leaks(payload, frozenset({FOREIGN_UUID}))

    # Assert
    assert leaks == (Leak(path="$.ref", token=FOREIGN_UUID),)


def test_find_leaks_does_not_match_a_foreign_id_that_is_only_a_substring_of_a_token() -> None:
    # Arrange
    payload = {"a": "mch_10", "b": "xmch_1y"}

    # Act
    leaks = find_leaks(payload, frozenset({"mch_1"}))

    # Assert
    assert leaks == ()


def test_find_leaks_ignores_numbers_booleans_and_none() -> None:
    # Arrange
    payload = {"a": 1, "b": None, "c": True, "d": 2.5, "e": [None, False, 7]}

    # Act
    leaks = find_leaks(payload, frozenset({"1", "true", "none", "2.5", "false", "7"}))

    # Assert
    assert leaks == ()


def test_find_leaks_returns_leaks_in_walk_order() -> None:
    # Arrange: dict insertion order, depth first
    payload = {"z": "tok_a", "a": ["tok_b", {"k": "tok_c"}], "m": "tok_d"}

    # Act
    leaks = find_leaks(payload, frozenset({"tok_a", "tok_b", "tok_c", "tok_d"}))

    # Assert
    assert leaks == (
        Leak(path="$.z", token="tok_a"),
        Leak(path="$.a[0]", token="tok_b"),
        Leak(path="$.a[1].k", token="tok_c"),
        Leak(path="$.m", token="tok_d"),
    )


def test_find_leaks_reports_a_leaking_key_before_its_leaking_value() -> None:
    # Arrange
    payload = {"tok_a": "tok_b"}

    # Act
    leaks = find_leaks(payload, frozenset({"tok_a", "tok_b"}))

    # Assert
    assert [leak.token for leak in leaks] == ["tok_a", "tok_b"]


def test_find_leaks_reports_every_leaking_token_of_one_string() -> None:
    # Arrange
    payload = {"note": "tok_a and tok_b and fine"}

    # Act
    leaks = find_leaks(payload, frozenset({"tok_a", "tok_b"}))

    # Assert
    assert leaks == (Leak(path="$.note", token="tok_a"), Leak(path="$.note", token="tok_b"))


def test_leakage_metric_passes_with_score_one_when_there_are_no_leaks() -> None:
    # Arrange / Act
    result = leakage_metric(())

    # Assert
    assert (result.name, result.score, result.threshold, result.status) == (
        "cross_tenant_leakage",
        1.0,
        1.0,
        "passed",
    )


def test_leakage_metric_fails_with_score_zero_on_a_single_leak() -> None:
    # Arrange
    leaks = [Leak(path="$.evidence[1].ref", token="mch_foreign_secret")]

    # Act
    result = leakage_metric(leaks)

    # Assert
    assert isinstance(result, MetricResult)
    assert (result.name, result.score, result.threshold, result.status) == (
        "cross_tenant_leakage",
        0.0,
        1.0,
        "failed",
    )


def test_leakage_metric_detail_names_the_path_and_never_the_token() -> None:
    # Arrange
    leaks = [Leak(path="$.evidence[1].ref", token="mch_foreign_secret")]

    # Act
    result = leakage_metric(leaks)

    # Assert
    assert "$.evidence[1].ref" in result.detail
    assert "mch_foreign_secret" not in result.detail


def test_leakage_metric_detail_names_the_count_and_every_path() -> None:
    # Arrange
    leaks = [Leak(path="$.alpha", token="tok_one"), Leak(path="$.beta", token="tok_two")]

    # Act
    result = leakage_metric(leaks)

    # Assert
    assert "2" in result.detail
    assert "$.alpha" in result.detail
    assert "$.beta" in result.detail
    assert "tok_one" not in result.detail
    assert "tok_two" not in result.detail
