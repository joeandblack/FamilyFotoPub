"""Run installed Chrome against the real frontend with synthetic API responses.

No app backend, database, real media, or existing browser profile is used.
Requires an installed Chrome; installs nothing. Artifacts stay in temp/.
"""
import html
import os
import json
import re
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
WEB = Path(os.environ.get('FAMILYFOTO_WEB_ROOT',ROOT/'web')).resolve()
CHROME = Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/mobile':
            # Windows Chrome enforces a minimum top-level window width. A real
            # 390px browsing context exercises mobile CSS without that clamp.
            payload = b'''<!doctype html><html><head><title>390px fixture viewport</title></head>
<body style="margin:0"><iframe src="/" style="border:0;width:390px;height:844px;display:block"></iframe>
<script>addEventListener('message',event=>{if(event.origin!==location.origin)return;
const report=document.createElement('pre');report.id='browser-test-result';report.hidden=true;
report.textContent=event.data;document.body.append(report);});</script></body></html>'''
            content_type = 'text/html; charset=utf-8'
        elif path == '/':
            source = (WEB/'index.html').read_text(encoding='utf-8')
            fixture = (ROOT/'tests/browser-smoke.mjs').read_text(encoding='utf-8')
            payload = source.replace('<head>', '<head><script>'+fixture+'</script>', 1).encode()
            content_type = 'text/html; charset=utf-8'
        elif path.startswith('/web/'):
            target = (WEB/path[5:]).resolve()
            if not target.is_relative_to(WEB) or not target.is_file():
                self.send_error(404)
                return
            payload = target.read_bytes()
            content_type = 'text/css' if target.suffix == '.css' else 'text/javascript'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    if not CHROME.is_file():
        raise RuntimeError('Installed Chrome not found; no browser installed by this test')
    output = Path(tempfile.mkdtemp(prefix='browser-smoke-', dir=ROOT/'temp'))
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    reports = []
    try:
        for name, size in [('desktop', '1440,1000'), ('mobile', '800,1000')]:
            result = subprocess.run([
                str(CHROME), '--headless', '--no-first-run', '--no-default-browser-check',
                '--disable-background-networking', '--disable-component-update',
                '--disable-sync', '--metrics-recording-only',
                f'--user-data-dir={output/name}', f'--window-size={size}',
                '--force-device-scale-factor=1', '--virtual-time-budget=5000',
                f'--screenshot={output/(name+".png")}', '--dump-dom',
                f'http://127.0.0.1:{server.server_port}/'+('mobile' if name=='mobile' else '')],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=45, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            (output/(name+'.html')).write_text(result.stdout, encoding='utf-8')
            (output/(name+'.log')).write_text(result.stderr, encoding='utf-8')
            match = re.search(r'<pre id="browser-test-result"[^>]*>(.*?)</pre>', result.stdout, re.S)
            report = json.loads(html.unescape(match.group(1))) if match else {'ok':False,'error':'Browser produced no test report','exit_code':result.returncode}
            reports.append({'browser':'Installed Google Chrome (headless)', 'mode':name, **report})
    finally:
        server.shutdown()
        server.server_close()
    (output/'results.json').write_text(json.dumps(reports, indent=2), encoding='utf-8')
    print(json.dumps({'artifacts':str(output),'results':reports}, indent=2))
    return 0 if all(report['ok'] for report in reports) else 1


if __name__ == '__main__':
    raise SystemExit(main())
