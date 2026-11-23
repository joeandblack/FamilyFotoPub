"""Normal writable server retaining the existing People catalog during cutover."""
import json
import os
from pathlib import Path

# Not imported from app.config: that would fix config.DATA before main() points it at the shared directory.
ROOT = Path(__file__).resolve().parents[2]


def main():
    root = ROOT
    cfg = json.loads((root/'config/local.json').read_text(encoding='utf-8'))
    if cfg.get('live_existing_catalog') is not True:
        raise RuntimeError('Explicit existing-catalog deployment configuration required')
    data = Path(cfg['preview']['data_root']).resolve()
    if not (data/'people-preview.json').is_file():
        raise RuntimeError('Existing People data directory is unavailable')
    # Child import/conversion processes inherit the same runtime-state directory.
    os.environ['FAMILYFOTO_DATA_DIR'] = str(data)
    from app import api
    from face_detect.api.readonly_people import install
    install(api.app, cfg, root)
    from app.service.desktop import main as serve
    serve()


if __name__ == '__main__':
    main()
