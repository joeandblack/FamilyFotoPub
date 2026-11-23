"""Settings Users section in a real headless Chrome, with a synthetic API.

No app backend, database or account data is used. Skips when Chrome isn't installed.
"""
import html
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = (ROOT / 'web').resolve()
CHROME = Path(os.environ.get('FAMILYFOTO_CHROME') or (
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' if sys.platform == 'darwin'
    else r'C:\Program Files\Google\Chrome\Application\chrome.exe'))

# Runs inside the page: drives the real module, then reports what happened.
SCENARIO = r"""
import {setupUserManagement} from '/web/user-management.mjs';
import {setupAccountSecurity} from '/web/account-security.mjs';
import {setupStorageSettings} from '/web/storage-settings.mjs';
import {setupUploadSettings} from '/web/upload-settings.mjs';
import {setupBackupSettings} from '/web/backup-settings.mjs';
import {setupFaceService} from '/face_detect/web/face-service.mjs';
const calls=[],sleep=ms=>new Promise(r=>setTimeout(r,ms)),report={};
window.navigated=null;
const users=[{username:'admin',role:'admin',primary:true},{username:'guest',role:'guest',primary:false}];
const api=async(path,options)=>{
 if(!options)return {users,accounts:[]};
 const body=JSON.parse(options.body);calls.push({path,body});
 return {ok:true,users,signed_out:body.username==='admin'};
};
async function confirmDialog(){
 for(let i=0;i<50&&!document.querySelector('dialog[open]');i++)await sleep(20);
 document.querySelector('dialog[open] .confirm-dialog-actions button:last-child').click();await sleep(50);
}
try{
 const section=setupUserManagement({api,user:{username:'admin',role:'admin'},notice:()=>{},navigate:url=>{window.navigated=url;}});
 setupAccountSecurity({api,user:{username:'admin',role:'admin'},notice:()=>{}});
 setupStorageSettings({api,refresh:()=>{}});
 setupUploadSettings({api});setupBackupSettings({api});setupFaceService({api});
 report.sectionsClosed=[...document.querySelectorAll('#storage > details')].length===8&&
   [...document.querySelectorAll('#storage > details')].every(item=>!item.open&&item.querySelector(':scope > summary'));
 section.querySelector('summary').click();
 report.sectionExpands=section.open;
 await sleep(50);
 const reset=section.querySelector('[data-reset]'),saved=reset.querySelector('[data-saved]'),status=section.querySelector('[data-status]');
 reset.elements.admin_password.value='fixture-admin';
 reset.elements.username.value='admin';reset.elements.username.dispatchEvent(new Event('change'));
 report.boxShownForSelf=!saved.hidden;
 reset.requestSubmit();await sleep(50);
 report.untickedBlocked=calls.length===0&&status.textContent.includes('tick the box')&&!document.querySelector('dialog[open]');
 reset.elements.saved.checked=true;reset.querySelector('[data-generate]').click();
 report.newPasswordClearsTick=!reset.elements.saved.checked;
 reset.requestSubmit();await sleep(50);
 report.stillBlocked=calls.length===0;
 reset.elements.saved.checked=true;const ownPassword=reset.elements.password.value;
 reset.requestSubmit();await confirmDialog();
 report.selfResetSent=calls.length===1&&calls[0].body.username==='admin'&&calls[0].body.password===ownPassword;
 report.selfResetSignsOut=window.navigated==='/login';
 report.confirmationSentAndCleared=calls[0].body.admin_password==='fixture-admin'&&reset.elements.admin_password.value==='';
 window.navigated=null;
 reset.elements.username.value='guest';reset.elements.username.dispatchEvent(new Event('change'));
 report.boxHiddenForOthers=saved.hidden;
 reset.elements.admin_password.value='fixture-admin';
 const guestPassword=reset.elements.password.value;
 reset.requestSubmit();await confirmDialog();
 report.otherResetSent=calls.length===2&&calls[1].body.username==='guest'&&calls[1].body.password===guestPassword;
 report.otherResetStaysWithPasswordShown=window.navigated===null&&reset.elements.password.value===guestPassword;
 const create=section.querySelector('[data-create]');
 create.elements.username.value='newuser';create.elements.admin_password.value='fixture-admin';
 create.requestSubmit();await sleep(50);
 report.createConfirmation=calls.length===3&&calls[2].body.admin_password==='fixture-admin'&&create.elements.admin_password.value==='';
 report.ok=Object.values(report).every(Boolean);
}catch(error){report.ok=false;report.error=String(error);}
const out=document.createElement('pre');out.id='browser-test-result';out.textContent=JSON.stringify(report);document.body.append(out);
"""

STORAGE = re.search(r'<section id="storage".*?</section>', (WEB / "index.html").read_text(), re.S).group(0).replace(" hidden", "", 1)

PAGE = f"""<!doctype html><html data-theme="familyfoto"><meta charset="utf-8">
<link rel="stylesheet" href="/web/styles.css"><body><aside><nav></nav></aside><main>{STORAGE}</main>
<script type="module">{SCENARIO}</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/':
            body, kind = PAGE.encode(), 'text/html'
        else:
            file = (ROOT / path.lstrip('/')).resolve()
            if not path.startswith(('/web/', '/face_detect/web/')) or not file.is_relative_to(ROOT) or not file.is_file():
                self.send_error(404)
                return
            body = file.read_bytes()
            kind = 'text/javascript' if file.suffix in ('.mjs', '.js') else 'text/css' if file.suffix == '.css' else 'text/plain'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.end_headers()
        self.wfile.write(body)


@pytest.mark.skipif(not CHROME.is_file(), reason='Installed Chrome not found')
def test_self_reset_requires_the_new_password_to_be_saved(tmp_path):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = subprocess.run([
            str(CHROME), '--headless', '--no-first-run', '--no-default-browser-check',
            '--disable-background-networking', '--disable-component-update', '--disable-sync',
            f'--user-data-dir={tmp_path / "profile"}', '--virtual-time-budget=8000', '--dump-dom',
            f'http://127.0.0.1:{server.server_port}/'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    finally:
        server.shutdown()
        server.server_close()
    match = re.search(r'<pre id="browser-test-result"[^>]*>(.*?)</pre>', result.stdout, re.S)
    assert match, f'Browser produced no report (exit {result.returncode})'
    report = json.loads(html.unescape(match.group(1)))
    assert report.pop('ok') is True, report
