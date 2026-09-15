"""Validates a managed document's frontmatter mapping against C5's field contract."""

import datetime
import re

REQUIRED_FIELDS = [
    "id",
    "type",
    "title",
    "summary",
    "status",
    "created",
    "reviewed",
    "origin",
    "evidence",
    "kind",
    "authored_by",
    "retention",
]

EMPTY_ALLOWED = {"created", "reviewed", "summary"}

TEXT_FIELDS = {
    "id",
    "type",
    "title",
    "summary",
    "status",
    "kind",
    "authored_by",
    "retention",
    "source_identity",
    "original",
    "original_sha256",
}

STATUS_VALUES = {"draft", "incomplete", "complete", "superseded"}
KIND_VALUES = {"source", "synthesis", "query-output", "decision", "project-context", "unknown"}
AUTHORED_BY_VALUES = {"human", "agent", "mixed"}
RETENTION_VALUES = {"ephemeral", "durable"}

ID_RE = re.compile(r"^[0-9abcdefghjkmnpqrstvwxyz]{26}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REFERENCE_LIST_FIELDS = ["evidence", "derived_from", "conflicts_with", "supersedes"]


def _is_missing(value, field):
    if value is None:
        return field not in EMPTY_ALLOWED
    return bool(isinstance(value, str) and value == "" and field not in EMPTY_ALLOWED)


def _is_valid_date_value(value):
    if isinstance(value, datetime.datetime):
        return False
    if isinstance(value, datetime.date):
        return True
    if isinstance(value, str):
        if not DATE_RE.match(value):
            return False
        year, month, day = (int(part) for part in value.split("-"))
        try:
            datetime.date(year, month, day)
        except ValueError:
            return False
        return True
    return False


def _is_reference(value):
    if not isinstance(value, str) or value == "":
        return False
    if ID_RE.match(value):
        return True
    return bool(value.startswith(("http://", "https://")))


def validate_mapping(mapping, type_registry, zone):
    """Returns a list of (field_or_None, code) error tuples."""
    errors = []

    for field in REQUIRED_FIELDS:
        value = mapping.get(field)
        if field not in mapping or _is_missing(value, field):
            errors.append((field, "missing"))

    type_value = mapping.get("type")
    type_def = None
    type_known = "type" in mapping and not _is_missing(type_value, "type")
    if type_known:
        if not isinstance(type_value, str):
            errors.append(("type", "ambiguous_scalar"))
            type_known = False
        else:
            type_def = type_registry.get(type_value)
            if type_def is None:
                errors.append(("type", "unknown_type"))
            elif not type_def.allows_zone(zone):
                errors.append(("type", "wrong_zone"))

    _check_text_field(mapping, "id", errors, extra_format=_check_id_format)
    _check_text_field(mapping, "title", errors)
    _check_text_field(mapping, "summary", errors, extra_format=_check_summary_length)
    _check_enum_field(mapping, "status", STATUS_VALUES, errors)
    _check_enum_field(mapping, "kind", KIND_VALUES, errors)
    _check_enum_field(mapping, "authored_by", AUTHORED_BY_VALUES, errors)
    _check_enum_field(mapping, "retention", RETENTION_VALUES, errors)
    _check_date_field(mapping, "created", errors)
    _check_date_field(mapping, "reviewed", errors)
    _check_origin_field(mapping, errors)

    for field in REFERENCE_LIST_FIELDS:
        _check_reference_list_field(mapping, field, errors, required=(field == "evidence"))

    if type_def is not None:
        for extra_field in type_def.required:
            value = mapping.get(extra_field)
            if extra_field not in mapping or _is_missing(value, extra_field):
                errors.append((extra_field, "missing"))
            elif extra_field == "original_sha256":
                if not isinstance(value, str) or not SHA256_RE.match(value):
                    errors.append((extra_field, "bad_format"))
            elif extra_field in TEXT_FIELDS and not isinstance(value, str):
                errors.append((extra_field, "ambiguous_scalar"))
        for field, allowed in type_def.allowed_values.items():
            value = mapping.get(field)
            if field in mapping and not _is_missing(value, field) and value not in allowed:
                errors.append((field, "not_allowed"))

    _check_needs_review(mapping, type_def, errors)

    return errors


def _check_text_field(mapping, field, errors, extra_format=None):
    if field not in mapping:
        return
    value = mapping.get(field)
    if _is_missing(value, field):
        return
    if value is None:
        value = ""
    if not isinstance(value, str):
        errors.append((field, "ambiguous_scalar"))
        return
    if extra_format is not None:
        extra_format(field, value, errors)


def _check_id_format(field, value, errors):
    if not ID_RE.match(value):
        errors.append((field, "bad_format"))


def _check_summary_length(field, value, errors):
    if len(value) > 300:
        errors.append((field, "too_long"))


def _check_enum_field(mapping, field, allowed_values, errors):
    if field not in mapping:
        return
    value = mapping.get(field)
    if _is_missing(value, field):
        return
    if not isinstance(value, str):
        errors.append((field, "ambiguous_scalar"))
        return
    if value not in allowed_values:
        errors.append((field, "not_allowed"))


def _check_date_field(mapping, field, errors):
    if field not in mapping:
        return
    value = mapping.get(field)
    if _is_missing(value, field):
        return
    if value is None or (isinstance(value, str) and value == ""):
        return
    if not _is_valid_date_value(value):
        errors.append((field, "bad_format"))


def _check_origin_field(mapping, errors):
    if "origin" not in mapping:
        return
    value = mapping.get("origin")
    if _is_missing(value, "origin"):
        return
    if isinstance(value, str):
        if value not in ("authored", "unknown"):
            errors.append(("origin", "bad_format"))
        return
    if isinstance(value, list):
        if len(value) == 0:
            errors.append(("origin", "bad_format"))
            return
        for entry in value:
            if not isinstance(entry, dict) or "ref" not in entry or not isinstance(entry.get("ref"), str) or entry.get("ref") == "":
                errors.append(("origin", "bad_format"))
                continue
            retrieved = entry.get("retrieved")
            if retrieved is None or (isinstance(retrieved, str) and retrieved == ""):
                continue
            if not _is_valid_date_value(retrieved):
                errors.append(("origin", "bad_format"))
        return
    errors.append(("origin", "ambiguous_scalar"))


def _check_reference_list_field(mapping, field, errors, required):
    if field not in mapping:
        return
    value = mapping.get(field)
    if value is None:
        return
    if not isinstance(value, list):
        errors.append((field, "bad_format"))
        return
    for entry in value:
        if not _is_reference(entry):
            errors.append((field, "bad_format"))


def _uncertain_fields(mapping, type_def):
    uncertain = set()
    created = mapping.get("created")
    if created is None or (isinstance(created, str) and created == ""):
        uncertain.add("created")
    reviewed = mapping.get("reviewed")
    if reviewed is None or (isinstance(reviewed, str) and reviewed == ""):
        uncertain.add("reviewed")
    if mapping.get("kind") == "unknown":
        uncertain.add("kind")
    if mapping.get("type") == "unclassified":
        uncertain.add("type")
    summary = mapping.get("summary")
    if summary is None or (isinstance(summary, str) and summary == ""):
        uncertain.add("summary")
    origin = mapping.get("origin")
    if origin == "unknown":
        uncertain.add("origin")
    elif isinstance(origin, list):
        for entry in origin:
            if isinstance(entry, dict):
                retrieved = entry.get("retrieved")
                if retrieved is None or (isinstance(retrieved, str) and retrieved == ""):
                    uncertain.add("origin")
                    break
    if type_def is not None:
        for field, marker in type_def.uncertainty_values.items():
            if marker is None:
                if field not in mapping:
                    uncertain.add(field)
            else:
                if mapping.get(field) == marker:
                    uncertain.add(field)
    return uncertain


def _check_needs_review(mapping, type_def, errors):
    uncertain = _uncertain_fields(mapping, type_def)
    needs_review = mapping.get("needs_review")
    if needs_review is None or needs_review == []:
        if uncertain:
            errors.append(("needs_review", "needs_review_mismatch"))
        return
    if not isinstance(needs_review, list) or not all(isinstance(item, str) for item in needs_review):
        errors.append(("needs_review", "bad_format"))
        return
    if len(needs_review) != len(set(needs_review)):
        errors.append(("needs_review", "bad_format"))
        return
    if set(needs_review) != uncertain:
        errors.append(("needs_review", "needs_review_mismatch"))
