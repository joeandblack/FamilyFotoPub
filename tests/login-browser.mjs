import {chromePath} from './chrome.mjs';
// Disposable login responses; no credentials or live server used.
import {createServer} from 'node:http';
import {readFileSync,writeFileSync,mkdtempSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const root=resolve(process.env.FAMILYFOTO_WEB_ROOT||'web'),out=mkdtempSync(resolve('temp/daisy-login-'));
let attempts=0;
const commands=[];
const server=createServer((req,res)=>{
 if(req.url==='/api/login'){
  const limited=++attempts===2;res.writeHead(limited?429:401,{'Content-Type':'application/json',...(limited?{'Retry-After':'1'}:{})});
  res.end(JSON.stringify(limited?{error:'Too many sign-in attempts. Try again in 1 second.',retry_after:1}:{error:'Incorrect username or password'}));return;
 }
 if(req.url==='/api/auth/accounts'){res.setHeader('Content-Type','application/json');res.end('{"accounts":["admin","guest"]}');return;}
 if(req.url.startsWith('/api/auth/')){
  let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{
   commands.push({path:req.url,body:body?JSON.parse(body):null});
   res.setHeader('Content-Type','application/json');res.end('{"ok":true,"revoked":2}');
  });return;
 }
 if(req.url.startsWith('/account')){
  const role=req.url.includes('guest')?'guest':'admin';
  res.setHeader('Content-Type','text/html');
  res.end(`<!doctype html><html data-theme="familyfoto"><meta name="viewport" content="width=device-width,initial-scale=1">
   <link rel="stylesheet" href="/web/daisyui-5.7.50.css"><link rel="stylesheet" href="/web/styles.css">
   <body><div class="layout"><aside><h1>FamilyFoto</h1><nav><button>Settings</button></nav></aside>
   <main><header><h2>Settings</h2></header><section id="storage" class="details"></section></main></div>
   <script src="/web/lucide.min.js"></script><script type="module" src="/web/ui.mjs"></script>
   <script type="module">import {setupAccountSecurity} from '/web/account-security.mjs';
   setupAccountSecurity({user:{username:'${role}',role:'${role}'},notice:message=>window.lastNotice=message,navigate:url=>window.navigationTarget=url,
    api:async(path,options)=>{const r=await fetch(path,options);return r.json();}});</script></body></html>`);return;
 }
 const file=join(root,req.url==='/'?'login.html':req.url.replace(/^\/web\//,''));
 if(!file.startsWith(root)||!existsSync(file)){res.writeHead(404);res.end();return;}
 res.setHeader('Content-Type',file.endsWith('.css')?'text/css':file.endsWith('.mjs')?'text/javascript':'text/html');res.end(readFileSync(file));
});await new Promise(r=>server.listen(0,'127.0.0.1',r));
const url=`http://127.0.0.1:${server.address().port}/`,profile=join(out,'profile'),pause=ms=>new Promise(r=>setTimeout(r,ms));
const browser=spawn(chromePath(),['--headless','--no-first-run','--disable-background-networking','--remote-debugging-port=0',`--user-data-dir=${profile}`,'--window-size=1280,900',url],{windowsHide:true,stdio:'ignore'});
let ws;
try{
 for(let n=0;n<100&&!existsSync(join(profile,'DevToolsActivePort'));n++)await pause(100);
 const port=readFileSync(join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];let pages;
 for(let n=0;n<100;n++){pages=await(await fetch(`http://127.0.0.1:${port}/json/list`)).json();if(pages.some(p=>p.url===url))break;await pause(100);}
 ws=new WebSocket(pages.find(p=>p.url===url).webSocketDebuggerUrl);await new Promise(r=>ws.addEventListener('open',r,{once:true}));
 let next=0;const pending=new Map();ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(m.error.message)):p.resolve(m.result);}});
 const cdp=(method,params={})=>new Promise((resolve,reject)=>{const id=++next;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
 const evaluate=async expression=>{const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.text);return r.result.value;};
 for(let n=0;n<100&&!await evaluate("document.querySelector('[name=username]')?.classList.contains('input')");n++)await pause(50);
 assert(await evaluate("document.querySelector('[type=submit]').classList.contains('btn-primary')"));
 await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
 assert(await evaluate("document.activeElement.name==='username'&&document.activeElement.matches(':focus-visible')&&parseFloat(getComputedStyle(document.activeElement).outlineWidth)>0"));
 await evaluate("document.querySelector('[name=username]').value='test';document.querySelector('[name=password]').value='test';document.getElementById('loginForm').requestSubmit()");
 for(let n=0;n<100&&!await evaluate("!document.getElementById('error').hidden");n++)await pause(50);
 assert.equal(await evaluate("document.getElementById('error').textContent"),'Incorrect username or password');
 assert(await evaluate("!document.querySelector('[type=submit]').disabled"));
 await evaluate("document.getElementById('loginForm').requestSubmit()");
 for(let n=0;n<100&&!await evaluate("document.getElementById('error').textContent.includes('Too many')");n++)await pause(20);
 assert(await evaluate("document.querySelector('[type=submit]').disabled"));
 writeFileSync(join(out,'login-desktop.png'),Buffer.from((await cdp('Page.captureScreenshot')).data,'base64'));
 await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 assert(await evaluate("document.documentElement.scrollWidth<=innerWidth"));
 writeFileSync(join(out,'login-mobile.png'),Buffer.from((await cdp('Page.captureScreenshot')).data,'base64'));
 await pause(1100);
 assert(await evaluate("!document.querySelector('[type=submit]').disabled"));
 const wait=async expression=>{for(let i=0;i<100;i++){if(await evaluate(expression))return;await pause(50);}throw Error('Timeout: '+expression);};
 await cdp('Page.navigate',{url:url+'account'});
 await wait("document.querySelectorAll('#accountSecurity select option').length===2");
 for(const width of [1280,390]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:844,deviceScaleFactor:1,mobile:width<500});
  assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'));
  writeFileSync(join(out,`account-${width}.png`),Buffer.from((await cdp('Page.captureScreenshot')).data,'base64'));
 }
 await evaluate("document.querySelector('[data-signout-all]').click()");
 await wait("!!document.querySelector('dialog[open]')");
 await evaluate("document.querySelector('.confirm-dialog-actions button').click()");
 assert.equal(commands.length,0);
 await evaluate("document.querySelector('[data-signout-all]').click()");
 await wait("!!document.querySelector('dialog[open]')");
 await evaluate("document.querySelector('.confirm-dialog-actions button:last-child').click()");
 await wait("window.navigationTarget==='/login'");
 assert.equal(commands.at(-1).path,'/api/auth/logout-all');
 await evaluate("window.navigationTarget=null;document.querySelector('#accountSecurity select').value='guest';document.querySelector('[data-revoke]').click()");
 await wait("!!document.querySelector('dialog[open]')");
 await evaluate("document.querySelector('.confirm-dialog-actions button:last-child').click()");
 await wait("document.querySelector('[data-status]').textContent==='Signed out 2 sessions.'");
 assert.deepEqual(commands.at(-1),{path:'/api/auth/revoke',body:{username:'guest'}});
 assert.equal(await evaluate('window.navigationTarget'),null);
 await evaluate("document.querySelector('aside > button').click()");
 await wait("window.navigationTarget==='/login'");
 assert.equal(commands.at(-1).path,'/api/auth/logout');
 await cdp('Page.navigate',{url:url+'account?guest'});
 await wait("!!document.querySelector('[data-signout-all]')");
 assert(await evaluate("document.querySelector('[data-admin]').hidden"));
 console.log(JSON.stringify({ok:true,browser:'Installed Windows Chrome',checks:['Failed login and throttle wait','Sidebar sign out','Sign out all confirmation/cancel','Admin account revocation','Guest controls','Desktop/mobile layout'],artifacts:out}));
 await cdp('Browser.close');
}finally{ws?.close();browser.kill();server.close();}
