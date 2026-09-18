"""Detects and parses YAML frontmatter blocks per specification C4."""


class FrontmatterBlock:
    def __init__(self, block_text, body_text):
        self.block_text = block_text
        self.body_text = body_text


UNCLOSED = object()


def split_frontmatter(text):
    """Returns None (no frontmatter), UNCLOSED, or a FrontmatterBlock."""
    body = text
    body = body.removeprefix("﻿")
    lines = body.splitlines(keepends=True)
    if not lines:
        return None
    first_line = lines[0].rstrip("\r\n")
    if first_line != "---":
        return None
    close_idx = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            close_idx = i
            break
    if close_idx is None:
        return UNCLOSED
    block_text = "".join(lines[1:close_idx])
    remaining_body = "".join(lines[close_idx + 1 :])
    return FrontmatterBlock(block_text, remaining_body)


def scan_features(block_text, yaml_module):
    """Scans a frontmatter block's YAML events for editing-unsupported features.

    Returns (features: set[str] | None, doc_count: int). features is None if
    the block cannot even be parsed into events (a YAML syntax error).
    """
    features = set()
    doc_count = 0
    seen_root_mapping = False
    try:
        for event in yaml_module.parse(block_text, Loader=yaml_module.SafeLoader):
            if isinstance(event, yaml_module.DocumentStartEvent):
                doc_count += 1
            elif isinstance(event, yaml_module.AliasEvent):
                features.add("alias")
            if isinstance(
                event,
                (yaml_module.MappingStartEvent, yaml_module.SequenceStartEvent, yaml_module.ScalarEvent),
            ):
                if getattr(event, "anchor", None):
                    features.add("anchor")
                tag = getattr(event, "tag", None)
                implicit = getattr(event, "implicit", None)
                if tag is not None:
                    is_implicit = implicit[0] if isinstance(implicit, tuple) else implicit
                    if not is_implicit:
                        features.add("tag")
                if isinstance(event, yaml_module.MappingStartEvent) and not seen_root_mapping:
                    seen_root_mapping = True
                    if getattr(event, "flow_style", False):
                        features.add("flow_mapping")
    except yaml_module.YAMLError:
        return None, 0
    if doc_count > 1:
        features.add("multiple_documents")
    return features, doc_count


# --- Canonical written form (C4) -------------------------------------------

DATE_FIELDS = {"created", "reviewed", "fresh_until", "first_seen", "decided"}

# Fixed field order for the properties this story's canonical form knows
# about; any other key present in a mapping is appended afterward in sorted
# order, so a type-specific field is still written deterministically.
_FIELD_ORDER = [
    "kb", "id", "type", "title", "summary", "status", "created", "reviewed",
    "origin", "evidence", "kind", "authored_by", "retention",
    "fresh_until", "first_seen", "needs_review", "supersedes", "superseded_by",
    "previous_version", "derived_from", "conflicts_with",
    "independence_assessment", "claim_checks", "tags",
    "source_identity", "original", "original_sha256", "attachments",
]


def _quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _is_empty_date(value):
    return value is None or value == ""


def _render_scalar_field_line(field, value):
    if field == "kb":
        return f"kb: {int(value)}"
    if field in DATE_FIELDS:
        if _is_empty_date(value):
            return f"{field}:"
        return f"{field}: {value}"
    if value is None or value == "":
        return f'{field}: ""'
    if isinstance(value, str):
        return f"{field}: {_quote(value)}"
    # Anything else (shouldn't normally occur for a scalar field) is
    # rendered via its string form, quoted -- never left unquoted, so a
    # stray non-string scalar can never be misread as something else.
    return f"{field}: {_quote(str(value))}"


def _render_list_field_lines(field, value):
    if field == "origin":
        lines = [f"{field}:"]
        for entry in value:
            ref = entry.get("ref", "")
            lines.append(f'  - ref: {_quote(ref)}')
            retrieved = entry.get("retrieved")
            if _is_empty_date(retrieved):
                lines.append("    retrieved:")
            else:
                lines.append(f"    retrieved: {retrieved}")
        return lines
    if not value:
        return [f"{field}: []"]
    lines = [f"{field}:"]
    for item in value:
        lines.append(f"  - {_quote(item)}")
    return lines


def render_field_lines(field, value):
    """Renders one property's canonical lines (C4), key line first."""
    if field == "origin" and isinstance(value, str):
        return [f"{field}: {_quote(value)}"]
    if isinstance(value, list):
        return _render_list_field_lines(field, value)
    return [_render_scalar_field_line(field, value)]


def render_canonical_block(mapping):
    """Renders a full frontmatter block (the text between the `---` markers,
    each line LF-terminated) for `mapping`, in canonical form (C4)."""
    known = [f for f in _FIELD_ORDER if f in mapping]
    extra = sorted(f for f in mapping if f not in _FIELD_ORDER)
    lines = []
    for field in known + extra:
        lines.extend(render_field_lines(field, mapping[field]))
    return "".join(line + "\n" for line in lines)


# --- Targeted property replacement (C4) -------------------------------------


class UnsupportedFrontmatter(Exception):
    """Raised when a property's span cannot be safely replaced in place."""


def _line_has_comment(line):
    in_squote = False
    in_dquote = False
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if in_dquote:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_dquote = False
            i += 1
            continue
        if in_squote:
            if c == "'":
                if i + 1 < n and line[i + 1] == "'":
                    i += 2
                    continue
                in_squote = False
            i += 1
            continue
        if c == '"':
            in_dquote = True
        elif c == "'":
            in_squote = True
        elif c == "#":
            if i == 0 or line[i - 1] in " \t":
                return True
        i += 1
    return False


def compute_property_spans(block_text, yaml_module):
    """Returns {key: (start_line, end_line)} (0-indexed, inclusive) for every
    top-level property in `block_text`, using the parser's node positions
    (C4): a span runs from the property's key line through the last
    non-blank, non-comment line of its value, covering zero-indented block
    sequences. Raises UnsupportedFrontmatter if the block cannot be composed
    into a single mapping node (callers are expected to have already checked
    scan_features for anchors/aliases/tags/flow-style/multiple documents)."""
    try:
        node = yaml_module.compose(block_text, Loader=yaml_module.SafeLoader)
    except yaml_module.YAMLError as exc:
        raise UnsupportedFrontmatter(str(exc)) from exc
    if node is None:
        return {}
    if not isinstance(node, yaml_module.MappingNode):
        raise UnsupportedFrontmatter("frontmatter is not a mapping")

    lines = block_text.splitlines()
    spans = {}
    for key_node, value_node in node.value:
        key = key_node.value
        start_line = key_node.start_mark.line
        raw_end_line = value_node.end_mark.line
        if value_node.end_mark.column == 0:
            end_line = raw_end_line - 1
        else:
            end_line = raw_end_line
        if end_line < start_line:
            end_line = start_line
        while end_line > start_line and (
            lines[end_line].strip() == "" or lines[end_line].lstrip().startswith("#")
        ):
            end_line -= 1
        spans[key] = (start_line, end_line)
    return spans


def span_has_comment(block_lines, start_line, end_line):
    return any(_line_has_comment(block_lines[i]) for i in range(start_line, end_line + 1))


def replace_property(block_text, yaml_module, field, new_value):
    """Replaces an existing property's span with its canonical rendering
    (C4's targeted property replacement). Returns the new block_text.
    Raises UnsupportedFrontmatter if the field is absent, its span holds a
    comment, or its key/value lines carry a trailing comment."""
    spans = compute_property_spans(block_text, yaml_module)
    if field not in spans:
        raise UnsupportedFrontmatter(f"field {field!r} not present")
    start_line, end_line = spans[field]
    block_lines = block_text.splitlines()
    if span_has_comment(block_lines, start_line, end_line):
        raise UnsupportedFrontmatter(f"span for {field!r} holds a comment")
    new_lines = render_field_lines(field, new_value)
    result_lines = block_lines[:start_line] + new_lines + block_lines[end_line + 1 :]
    return "".join(line + "\n" for line in result_lines)
