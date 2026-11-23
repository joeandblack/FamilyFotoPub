import json
import copy
import threading
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('FAMILYFOTO_DATA_DIR', ROOT / 'data')).resolve()
LOGS = Path(os.environ.get('FAMILYFOTO_LOG_DIR', ROOT / 'logs')).resolve()
TEMP = Path(os.environ.get('FAMILYFOTO_TEMP_DIR', ROOT / 'temp')).resolve()


def configuration_root():
    return Path(os.environ.get('FAMILYFOTO_CONFIG_DIR', ROOT / 'config')).resolve()

_lock = threading.RLock()
_cached_signature = None
_cached_config = None
_initialized_directories = None


def initialize_runtime():
    """Prepare local application directories once, including for CLI workers."""
    global _initialized_directories
    directories = (DATA, LOGS, TEMP, DATA / 'thumbnails')
    with _lock:
        from .backup.admission import hold_process_lease
        hold_process_lease(DATA)
        if directories != _initialized_directories:
            for directory in directories:
                directory.mkdir(parents=True, exist_ok=True)
            _initialized_directories = directories


def _signature(path):
    stat = path.stat()
    return (path, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

def load_config():
    """Reload changed settings; never share mutable config between callers."""
    global _cached_signature, _cached_config
    path = configuration_root() / 'local.json'
    with _lock:
        signature = _signature(path)
        if signature != _cached_signature:
            # Atomic file replacement is normal. Do not cache a read spanning a save.
            for attempt in range(3):
                config = json.loads(path.read_text(encoding='utf-8'))
                if config.get('recovery', {}).get('activation_required') is True:
                    raise RuntimeError('Recovered library is validation-only; storage and activation checks are required')
                after = _signature(path)
                if after == signature:
                    break
                signature = after
            else:
                raise RuntimeError('Settings changed repeatedly while being read')
            for role in ('source', 'destination'):
                config.setdefault(role, {})
                if not isinstance(config[role], dict):
                    raise ValueError(f'{role} must be a storage object')
            if config['source'].get('root') and config['destination'].get('root'):
                source = Path(config['source']['root']).resolve()
                dest = Path(config['destination']['root']).resolve()
                if source == dest or source in dest.parents or dest in source.parents:
                    raise ValueError('Source and destination must be separate trees.')
            _cached_config, _cached_signature = config, signature
        initialize_runtime()
        return copy.deepcopy(_cached_config)


def require_destination(config):
    if not config.get('destination', {}).get('root'):
        raise ValueError('Choose the library destination in Imports before starting an import or upload.')
