"""config.yaml must mirror settings.DEFAULTS exactly (the app also runs outside HA on the defaults)."""
import collections
import os

import yaml

from settings import DEFAULTS

CONFIG = os.path.join(os.path.dirname(__file__), "..", "config.yaml")


class _NoDuplicates(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    keys = [loader.construct_object(k) for k, _ in node.value]
    dups = [k for k, n in collections.Counter(keys).items() if n > 1]
    assert not dups, f"duplicate keys in config.yaml: {dups}"
    return loader.construct_mapping(node, deep)


_NoDuplicates.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def test_options_mirror_defaults_and_every_option_has_a_schema():
    with open(CONFIG) as f:
        cfg = yaml.load(f, _NoDuplicates)
    assert cfg["options"] == DEFAULTS
    assert set(cfg["options"]) == set(cfg["schema"])
