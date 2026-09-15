"""Loads the vendored PyYAML copy and refuses to run against any other copy."""

import os
import sys

REQUIRED_VERSION = "6.0.3"


class VendoredDependencyError(Exception):
    pass


def load(base_dir):
    """Puts vendor/ first on sys.path and imports the vendored yaml module.

    Raises VendoredDependencyError if the module actually loaded is not the
    vendored copy at the expected version.
    """
    vendor_dir = os.path.join(base_dir, "vendor")
    sys.path.insert(0, vendor_dir)
    try:
        import yaml
    except ImportError as exc:
        raise VendoredDependencyError(f"could not import yaml from {vendor_dir}: {exc}") from exc

    vendor_real = os.path.realpath(vendor_dir)
    module_file = getattr(yaml, "__file__", None)
    module_real = os.path.realpath(module_file) if module_file else ""
    version = getattr(yaml, "__version__", None)

    if not module_real.startswith(vendor_real + os.sep) or version != REQUIRED_VERSION:
        raise VendoredDependencyError(
            f"loaded yaml module is not the vendored copy at {vendor_real} (file={module_file!r} version={version!r})"
        )

    return yaml


def make_duplicate_safe_loader(yaml_module):
    """Builds a SafeLoader subclass that rejects duplicate mapping keys."""

    class DuplicateSafeLoader(yaml_module.SafeLoader):
        def construct_mapping(self, node, deep=False):
            if not isinstance(node, yaml_module.MappingNode):
                raise yaml_module.constructor.ConstructorError(
                    None, None, "expected a mapping node", node.start_mark
                )
            merge_tag = "tag:yaml.org,2002:merge"
            own_key_ids = {id(key_node) for key_node, _ in node.value if key_node.tag != merge_tag}
            self.flatten_mapping(node)
            mapping = {}
            seen_own_keys = set()
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                try:
                    hash(key)
                except TypeError as exc:
                    raise yaml_module.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        f"found unhashable key ({exc})",
                        key_node.start_mark,
                    )
                # A key merged in via << is not a duplicate of the mapping's own
                # explicit key that overrides it, nor of another merge source's
                # matching key; only two of the mapping's own explicit keys can
                # collide (standard YAML 1.1 merge-key semantics).
                if id(key_node) in own_key_ids:
                    if key in seen_own_keys:
                        raise yaml_module.constructor.ConstructorError(
                            "while constructing a mapping",
                            node.start_mark,
                            f"found duplicate key: {key!r}",
                            key_node.start_mark,
                        )
                    seen_own_keys.add(key)
                value = self.construct_object(value_node, deep=deep)
                mapping[key] = value
            return mapping

    return DuplicateSafeLoader
