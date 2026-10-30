"""Parse model JSON without guessing missing values or publication decisions."""
from __future__ import annotations

import json
import re


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("nonfinite_number")


def _loads(text):
    return json.loads(text, object_pairs_hook=_object, parse_constant=_constant)


def parse_object(text: str, *, visual_repairs: bool = False):
    text = text.strip().lstrip("\ufeff").strip()
    fence = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fence:
        text = fence[1]
    try:
        return _loads(text), ()
    except json.JSONDecodeError:
        if not visual_repairs:
            raise
    # Vision descriptions only: escape literal control characters in strings
    # and drop trailing commas outside strings. Never insert a quote, key,
    # value, closing delimiter, permission, or approval verdict.
    output, repairs = [], set()
    in_string, escaped = False, False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            elif ord(char) < 32:
                output.append(json.dumps(char)[1:-1])
                repairs.add("string_control_character")
                continue
        elif char == '"':
            in_string = True
        elif char == ",":
            following = index + 1
            while following < len(text) and text[following] in " \t\r\n":
                following += 1
            if following < len(text) and text[following] in "]}":
                repairs.add("trailing_comma")
                continue
        output.append(char)
    return _loads("".join(output)), tuple(sorted(repairs))
