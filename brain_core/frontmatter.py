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
