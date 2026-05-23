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


def test_float_or_zero_rejects_nan(bag):
    # NaN / inf parse cleanly out of float() but poison downstream
    # math and comparisons (every NaN > x returns False, every NaN math
    # returns NaN). _float_or_zero is the chokepoint between Polymarket
    # API payloads and our display/PnL/size code — collapse them to 0.
    assert bag._float_or_zero("nan") == 0.0
    assert bag._float_or_zero("inf") == 0.0
    assert bag._float_or_zero("-inf") == 0.0
    assert bag._float_or_zero(float("nan")) == 0.0
    assert bag._float_or_zero(float("inf")) == 0.0


def test_optional_float_returns_none_on_garbage(bag):
    assert bag._optional_float("not a number") is None
    assert bag._optional_float(None) is None


def test_optional_float_parses_valid(bag):
    assert bag._optional_float("0.42") == 0.42


def test_optional_float_rejects_nan_inf(bag):
    # Found by Codex pass-4: _optional_float feeds _build_market's
    # bestBid / bestAsk parsing. NaN survives float() and the
    # downstream <0 / >1 / bid>=ask checks all evaluate False for NaN,
    # so a NaN-priced market would silently land in the candidate list.
    assert bag._optional_float("nan") is None
    assert bag._optional_float("inf") is None
    assert bag._optional_float("-inf") is None
    assert bag._optional_float(float("nan")) is None
    assert bag._optional_float(float("inf")) is None


def test_parse_token_ids_list_passthrough(bag):
    assert bag._parse_token_ids(["a", "b"]) == ["a", "b"]


def test_parse_token_ids_from_json_string(bag):
    # Gamma API often serializes the clobTokenIds field as a JSON string.
    assert bag._parse_token_ids('["yes-token", "no-token"]') == ["yes-token", "no-token"]


def test_parse_token_ids_returns_empty_on_garbage(bag):
    assert bag._parse_token_ids("not json") == []
    assert bag._parse_token_ids(None) == []
    assert bag._parse_token_ids("") == []


def test_parse_token_ids_rejects_scalar_string_payload(bag):
    # Bug found by Codex pass-4: a JSON-encoded scalar string like
    # '"abcdef"' previously char-iterated into ['a','b','c',...],
    # then _build_market would happily assign yes_id='a', no_id='b'
    # into the order path. _parse_token_ids must require a list.
    assert bag._parse_token_ids('"abcdef"') == []


def test_parse_token_ids_rejects_dict_payload(bag):
    assert bag._parse_token_ids('{"yes": "y", "no": "n"}') == []


def test_parse_token_ids_drops_empty_entries(bag):
    assert bag._parse_token_ids('["yes-id", "", null]') == ["yes-id"]


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
