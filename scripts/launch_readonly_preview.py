"""Launch the opt-in Windows preview with a Low Integrity primary token."""
import json
from pathlib import Path
import socket
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.service.windows_readonly import restrict_process
from app.service.readonly_preview import scratch_root


def main():
    limited='--limited-edit' in sys.argv
    cfg=json.loads((ROOT/'config'/('limited-edit-preview.json' if limited else 'read-only-preview.json')).read_text(encoding='utf-8'))
    if cfg.get('read_only_preview') is not True:raise RuntimeError('Preview configuration required')
    with socket.socket() as sock:
        sock.bind((cfg['host'],cfg['port']))
    writable=Path(cfg['preview']['writable_root']).resolve()
    if writable!=scratch_root(ROOT):raise RuntimeError('Unsafe preview scratch path')
    (writable/'STOP').unlink(missing_ok=True)
    command=[sys.executable,'-m','app.service.readonly_preview']+(['--limited-edit'] if limited else [])
    pid=restrict_process(command,ROOT,writable/'logs/server.log')
    (writable/'server.pid').write_text(str(pid))
    print(('Metadata-only' if limited else 'Read-only')+' preview started with Windows Low Integrity; PID '+str(pid))


if __name__=='__main__':main()
