"""Test-only YAML 1.2 core-schema scalar resolver (P9, Owner authorisation
A2/R1, 2026-09-18): the closed dependency set pins one vendored YAML 1.1
parser (C1), so proving persist's canonical written form (C4) also reads
correctly under YAML 1.2 needs a second reader. This is not a general 1.2
implementation: it covers only the one documented reader difference the
canonical form actually emits (C4) -- an unquoted `YYYY-MM-DD` date, which a
1.1 parser resolves to a date/timestamp and a 1.2 core-schema parser resolves
to a plain string. Every other value the canonical writer leaves unquoted
(`kb: 1`, empty scalars, `[]`) already resolves the same way under both
schemas, so no other resolver needs to change. It is test support, never
product code (see brain_core/vendor_yaml.py for the one parser persist
actually uses).
"""


def make_yaml12_loader(yaml_module):
    """Returns a SafeLoader subclass that resolves a plain `YYYY-MM-DD`
    scalar as `str` (1.2 core schema) instead of `timestamp` (1.1's
    extension), by dropping the timestamp implicit resolver so the scalar
    falls through to the plain `str` resolver instead."""

    class YAML12SafeLoader(yaml_module.SafeLoader):
        pass

    timestamp_tag = "tag:yaml.org,2002:timestamp"
    YAML12SafeLoader.yaml_implicit_resolvers = {
        first_char: [(tag, regexp) for tag, regexp in resolvers if tag != timestamp_tag]
        for first_char, resolvers in yaml_module.SafeLoader.yaml_implicit_resolvers.items()
    }
    return YAML12SafeLoader
