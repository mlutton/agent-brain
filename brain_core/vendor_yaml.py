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

    merge_tag = "tag:yaml.org,2002:merge"

    class DuplicateSafeLoader(yaml_module.SafeLoader):
        def flatten_mapping(self, node):
            # Every mapping passes through flatten_mapping before it is
            # constructed or merged, so this is where its explicit keys are
            # checked. The stock method merges in place: it removes the <<
            # pairs from node.value and prepends the merged pairs, so node.value
            # holds only the explicit pairs before the node's first flatten.
            # They are recorded then, and checked once, after the stock method
            # has run so that `=` keys are already retagged as strings. Later
            # visits of the same node (an anchor constructed and merged, or
            # merged from several places) find the merge already done and
            # re-check nothing.
            first_visit = not hasattr(node, "_explicit_pairs")
            if first_visit:
                node._explicit_pairs = [pair for pair in node.value if pair[0].tag != merge_tag]
            super().flatten_mapping(node)
            if first_visit:
                self._check_no_duplicate_keys(node)

        def _check_no_duplicate_keys(self, node):
            seen = set()
            for key_node, _value_node in node._explicit_pairs:
                key = self.construct_object(key_node, deep=False)
                try:
                    hash(key)
                except TypeError as exc:
                    raise yaml_module.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        f"found unhashable key ({exc})",
                        key_node.start_mark,
                    )
                if key in seen:
                    raise yaml_module.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        f"found duplicate key: {key!r}",
                        key_node.start_mark,
                    )
                seen.add(key)

    return DuplicateSafeLoader
