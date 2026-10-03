import re


def trim_slice(values: list[str] | None) -> list[str]:
    if not values:
        return []
    return [item.strip() for item in values if item is not None]


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return trim_slice(value.split(","))


def trim_manifest_field(field: str, width: int = 72) -> str:
    """Drop the continuation space inserted when a MANIFEST.MF value wraps."""
    index = width - 13
    if len(field) > index and field[index] == " ":
        field = field[:index] + field[index + 1 :]
    return field


def canonical_header(key: str) -> str:
    return "-".join(part.capitalize() for part in key.split("-"))


def header_get(headers: dict[str, str], key: str) -> str:
    return headers.get(canonical_header(key), "")


def parse_manifest(text: str) -> dict[str, str]:
    logical: list[str] = []
    current: str | None = None
    for line in text.splitlines():
        if line.startswith(" ") or line.startswith("\t"):
            if current is not None:
                current += line[1:]
            continue
        if line.strip() == "":
            break
        if current is not None:
            logical.append(current)
        current = line
    if current is not None:
        logical.append(current)

    headers: dict[str, str] = {}
    for line in logical:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[canonical_header(key.strip())] = value.lstrip()
    return headers


def parse_properties(text: str) -> dict[str, str]:
    """Parse a Java .properties document (UTF-8, as the upstream loader does)."""
    physical = text.splitlines()
    logical_lines: list[str] = []
    buffer = ""
    continuing = False
    for line in physical:
        if continuing:
            buffer += line.lstrip(" \t\f")
        else:
            stripped = line.lstrip(" \t\f")
            if not stripped or stripped[0] in "#!":
                continue
            buffer = stripped
        if _odd_trailing_backslash(buffer):
            buffer = buffer[:-1]
            continuing = True
            continue
        continuing = False
        logical_lines.append(buffer)
        buffer = ""
    if buffer:
        logical_lines.append(buffer)

    result: dict[str, str] = {}
    for line in logical_lines:
        key, value = _split_property(line)
        result[_unescape_property(key)] = _unescape_property(value)
    return result


def _odd_trailing_backslash(value: str) -> bool:
    count = 0
    for char in reversed(value):
        if char != "\\":
            break
        count += 1
    return count % 2 == 1


def _split_property(line: str) -> tuple[str, str]:
    escaped = False
    separator_at = None
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char in "=:" or char in " \t\f":
            separator_at = index
            break
    if separator_at is None:
        return line, ""
    key = line[:separator_at]
    rest = line[separator_at + 1 :]
    if line[separator_at] in " \t\f":
        rest = rest.lstrip(" \t\f")
        if rest.startswith("=") or rest.startswith(":"):
            rest = rest[1:].lstrip(" \t\f")
    else:
        rest = rest.lstrip(" \t\f")
    return key, rest


_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _unescape_property(value: str) -> str:
    def unicode_sub(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    value = _UNICODE_ESCAPE.sub(unicode_sub, value)
    output: list[str] = []
    escaped = False
    mapping = {"n": "\n", "r": "\r", "t": "\t", "f": "\f"}
    for char in value:
        if escaped:
            output.append(mapping.get(char, char))
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        output.append(char)
    if escaped:
        output.append("\\")
    return "".join(output)


def filter_ids(entity_id: str, included: list[str], excluded: list[str]) -> bool:
    """Return True when the id must be skipped."""
    if included and entity_id not in included:
        return True
    if excluded and entity_id in excluded:
        return True
    return False


def artifact_id_from_symbolic_name(value: str) -> str:
    cleaned = value.replace(" ", "")
    return cleaned.replace(";singleton:=true", "")


def bundle_type_to_artifact_type(bundle_type: str) -> str:
    if bundle_type == "IntegrationFlow":
        return "Integration"
    return bundle_type
