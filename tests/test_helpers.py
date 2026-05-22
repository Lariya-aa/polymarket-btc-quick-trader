"""Tests for the small string/number coercion helpers.

Each of these is called dozens of times in render_positions and other
hot paths. They must never raise on malformed input — they exist so the
GUI can't crash when an API returns an unexpected shape.
"""


def test_float_or_zero_handles_none(bag):
    assert bag._float_or_zero(None) == 0.0


def test_float_or_zero_handles_garbage_string(bag):
    assert bag._float_or_zero("abc") == 0.0


def test_float_or_zero_handles_numeric_string(bag):
    assert bag._float_or_zero("3.14") == 3.14


def test_float_or_zero_handles_int(bag):
    assert bag._float_or_zero(7) == 7.0


def test_optional_float_returns_none_on_garbage(bag):
    assert bag._optional_float("not a number") is None
    assert bag._optional_float(None) is None


def test_optional_float_parses_valid(bag):
    assert bag._optional_float("0.42") == 0.42


def test_parse_token_ids_list_passthrough(bag):
    assert bag._parse_token_ids(["a", "b"]) == ["a", "b"]


def test_parse_token_ids_from_json_string(bag):
    # Gamma API often serializes the clobTokenIds field as a JSON string.
    assert bag._parse_token_ids('["yes-token", "no-token"]') == ["yes-token", "no-token"]


def test_parse_token_ids_returns_empty_on_garbage(bag):
    assert bag._parse_token_ids("not json") == []
    assert bag._parse_token_ids(None) == []
    assert bag._parse_token_ids("") == []


def test_parse_token_ids_coerces_to_strings(bag):
    assert bag._parse_token_ids([1, 2]) == ["1", "2"]


def test_parse_datetime_handles_z_suffix(bag):
    dt = bag._parse_datetime("2026-05-22T10:00:00Z")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 5 and dt.day == 22
    assert dt.utcoffset() is not None  # tz-aware


def test_parse_datetime_handles_offset(bag):
    dt = bag._parse_datetime("2026-05-22T10:00:00+08:00")
    assert dt is not None
    assert dt.year == 2026


def test_parse_datetime_returns_none_on_garbage(bag):
    assert bag._parse_datetime("not a date") is None
    assert bag._parse_datetime(None) is None
    assert bag._parse_datetime("") is None


def test_book_level_value_dict(bag):
    assert bag._book_level_value({"price": "0.42", "size": "100"}, "price") == "0.42"


def test_book_level_value_object(bag):
    class _Level:
        price = "0.42"
        size = "100"
    assert bag._book_level_value(_Level(), "price") == "0.42"


def test_book_level_value_missing_returns_none(bag):
    assert bag._book_level_value({}, "price") is None
    assert bag._book_level_value(object(), "price") is None
