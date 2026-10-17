from app.canonical.type_conformity import (
    DEFAULT_NULL_SENTINELS,
    is_null_sentinel,
    parse_boolean,
    parse_integer_strict,
    parse_numeric,
)


def test_is_null_sentinel_matches_default_list_case_insensitively():
    assert is_null_sentinel("N/A")
    assert is_null_sentinel("n/a")
    assert is_null_sentinel("NULL")
    assert is_null_sentinel("-")
    assert is_null_sentinel("  na  ")


def test_is_null_sentinel_does_not_match_ordinary_values():
    assert not is_null_sentinel("Acme Corp")
    assert not is_null_sentinel("")
    assert not is_null_sentinel(None)


def test_is_null_sentinel_respects_custom_list():
    custom = frozenset({"missing"})
    assert is_null_sentinel("missing", custom)
    assert not is_null_sentinel("N/A", custom)  # not in the custom list


def test_parse_boolean_recognizes_true_false_and_yes_no_variants():
    assert parse_boolean("true") is True
    assert parse_boolean("False") is False
    assert parse_boolean("YES") is True
    assert parse_boolean("no") is False
    assert parse_boolean("Y") is True
    assert parse_boolean("n") is False


def test_parse_boolean_deliberately_excludes_1_and_0():
    """1/0 is genuinely ambiguous with an ordinary integer flag column —
    see the module docstring. Leaving these unparsed as boolean is what
    keeps a column of plain "1"/"0" values classified INTEGER."""
    assert parse_boolean("1") is None
    assert parse_boolean("0") is None


def test_parse_integer_strict_rejects_leading_zero_values():
    """The zip-code / ID trap: "02134" must not silently become 2134."""
    assert parse_integer_strict("02134") is None
    assert parse_integer_strict("00501") is None


def test_parse_integer_strict_accepts_ordinary_integers():
    assert parse_integer_strict("0") == 0
    assert parse_integer_strict("42") == 42
    assert parse_integer_strict("-7") == -7
    assert parse_integer_strict("+7") == 7


def test_parse_numeric_handles_currency_and_thousands_separators():
    assert parse_numeric("$1,200") == 1200.0
    assert parse_numeric("1,200") == 1200.0
    assert parse_numeric("€1,200.50") == 1200.50


def test_parse_numeric_handles_percentages():
    assert parse_numeric("45%") == 45.0


def test_parse_numeric_handles_scientific_notation():
    assert parse_numeric("1.5e10") == 1.5e10


def test_parse_numeric_rejects_non_numeric_text():
    assert parse_numeric("abc") is None
    assert parse_numeric("") is None
