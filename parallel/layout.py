"""One source of paths for new control state; legacy task records stay in place."""
from pathlib import Path


def control_root(inventory):
    return Path(inventory.host['data']) / 'parallel'


def executor_paths(inventory):
    root = control_root(inventory)
    return root / 'state/executor.sqlite', root / 'run/executor.sock', root / 'state/allocations'


def settings_path(etc):
    configured = Path(etc).parent.parent / 'parallel/etc/central.toml'
    return configured if configured.is_file() else Path(etc) / 'central.toml'
