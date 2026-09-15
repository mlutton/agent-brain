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
    value_tag = "tag:yaml.org,2002:value"

    class DuplicateSafeLoader(yaml_module.SafeLoader):
        def flatten_mapping(self, node):
            # Reimplements the base flatten_mapping (own pairs are appended
            # after every merged pair, so the mapping's own explicit keys are
            # always the tail of node.value after flattening -- position, not
            # node identity, decides which pairs are "own") and additionally
            # checks each merge source's own keys for duplicates: a source
            # used only inline via << is never otherwise passed through
            # construct_mapping, so its own duplicate keys would otherwise
            # resolve silently.
            own_pairs = [pair for pair in node.value if pair[0].tag != merge_tag]
            merge = []
            index = 0
            while index < len(node.value):
                key_node, value_node = node.value[index]
                if key_node.tag == merge_tag:
                    del node.value[index]
                    if isinstance(value_node, yaml_module.MappingNode):
                        self.flatten_mapping(value_node)
                        self._check_no_duplicate_keys(value_node)
                        merge.extend(value_node.value)
                    elif isinstance(value_node, yaml_module.SequenceNode):
                        submerge = []
                        for subnode in value_node.value:
                            if not isinstance(subnode, yaml_module.MappingNode):
                                raise yaml_module.constructor.ConstructorError(
                                    "while constructing a mapping",
                                    node.start_mark,
                                    f"expected a mapping for merging, but found {subnode.id}",
                                    subnode.start_mark,
                                )
                            self.flatten_mapping(subnode)
                            self._check_no_duplicate_keys(subnode)
                            submerge.append(subnode.value)
                        submerge.reverse()
                        for value in submerge:
                            merge.extend(value)
                    else:
                        raise yaml_module.constructor.ConstructorError(
                            "while constructing a mapping",
                            node.start_mark,
                            f"expected a mapping or list of mappings for merging, but found {value_node.id}",
                            value_node.start_mark,
                        )
                elif key_node.tag == value_tag:
                    key_node.tag = "tag:yaml.org,2002:str"
                    index += 1
                else:
                    index += 1
            if merge:
                node.value = merge + node.value
            node._own_pairs = own_pairs

        def _check_no_duplicate_keys(self, node):
            own_pairs = getattr(node, "_own_pairs", node.value)
            seen = set()
            for key_node, _value_node in own_pairs:
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

        def construct_mapping(self, node, deep=False):
            if not isinstance(node, yaml_module.MappingNode):
                raise yaml_module.constructor.ConstructorError(
                    None, None, "expected a mapping node", node.start_mark
                )
            self.flatten_mapping(node)
            self._check_no_duplicate_keys(node)
            mapping = {}
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
                value = self.construct_object(value_node, deep=deep)
                mapping[key] = value
            return mapping

    return DuplicateSafeLoader
