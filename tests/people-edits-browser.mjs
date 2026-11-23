import {chromePath} from './chrome.mjs';
// Real People handlers, disposable PostgreSQL schema, synthetic thumbnails.
import {readFileSync, writeFileSync, mkdtempSync, existsSync} from 'node:fs';
import {join} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const {url, named, unnamed, person} = JSON.parse(readFileSync(process.argv[2], 'utf8'));
assert.equal(new URL(url).hostname, '127.0.0.1');
assert.notEqual(new URL(url).port, '4340');
const out = mkdtempSync(join(process.env.FAMILYFOTO_BROWSER_OUTPUT, 'people-edits-'));
const profile = join(out, 'profile');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const chrome = spawn(chromePath(),
  ['--headless', '--no-first-run', '--disable-background-networking', '--remote-debugging-port=0',
   `--user-data-dir=${profile}`, '--window-size=1280,900', url], {windowsHide:true, stdio:'ignore'});
let ws;
try {
  for (let n=0; n<150 && !existsSync(join(profile, 'DevToolsActivePort')); n++) await pause(100);
  const port = readFileSync(join(profile, 'DevToolsActivePort'), 'utf8').split('\n')[0];
  let pages;
  for (let n=0; n<100; n++) {
    pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    if (pages.some(page => page.url === url)) break;
    await pause(100);
  }
  ws = new WebSocket(pages.find(page => page.url === url).webSocketDebuggerUrl);
  await new Promise(resolve => ws.addEventListener('open', resolve, {once:true}));
  let sequence=0;
  const pending=new Map();
  ws.addEventListener('message', event => {
    const data=JSON.parse(event.data);
    if (!data.id) return;
    const call=pending.get(data.id); pending.delete(data.id);
    data.error ? call.reject(Error(data.error.message)) : call.resolve(data.result);
  });
  const cdp=(method, params={}) => new Promise((resolve,reject) => {
    const id=++sequence;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));
  });
  const run=async expression => {
    const result=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
    if(result.exceptionDetails) throw Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result.value;
  };
  const wait=async expression => {
    for(let n=0;n<200;n++){if(await run(expression))return;await pause(50);}
    throw Error('Timed out: '+expression+'\n'+await run('document.body.innerText'));
  };
  const click=selector => run(`document.querySelector(${JSON.stringify(selector)}).click()`);
  const textClick=(selector,text) => run(`[...document.querySelectorAll(${JSON.stringify(selector)})].find(b=>b.textContent===${JSON.stringify(text)}).click()`);
  const api=path => run(`fetch('/api/people-preview'+${JSON.stringify(path)}).then(r=>r.json())`);
  const open=async id => {
    await click(`[data-group="${id}"] .group-photo`);
    await wait(`document.querySelector('#group-detail .card')?.dataset.group==='${id}'&&!document.querySelector('#group-detail .people-detail-loading:not([hidden])')`);
  };
  const checks=[];
  await wait(`document.querySelector('#groups [data-group="${named}"]')`);
  await run("window.browserErrors=[];addEventListener('error',e=>browserErrors.push(e.message));addEventListener('unhandledrejection',e=>browserErrors.push(String(e.reason)))");
  await open(named);
  await click('#group-detail .editable-person-name');
  await run("document.querySelector('#group-detail input[name=rename]').value='Browser renamed';document.querySelector('#group-detail form.inline-person-name').requestSubmit()");
  await wait("document.querySelector('#group-detail .editable-person-name')?.textContent==='Browser renamed'");
  assert.equal((await api(`/groups/${named}`)).group.name, 'Browser renamed');
  checks.push('Inline rename persists through real API');
  let group=(await api(`/groups/${named}`)).group;
  const cover=group.faces.find(face=>face.detection_id!==group.cover_detection_id).detection_id;
  await click(`#group-detail [data-face="${cover}"] .face-actions-trigger`);
  await run("document.querySelector(':popover-open').querySelector('button[aria-haspopup=menu],button[aria-label=\"Photo actions\"]')?.click()");
  // Context-menu opens the same action list without relying on icon markup.
  await run(`document.querySelector('#group-detail [data-face="${cover}"] .face-actions-trigger').dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}))`);
  await click('.choose-cover');
  await wait(`fetch('/api/people-preview/groups/${named}').then(r=>r.json()).then(d=>d.group.cover_detection_id===${cover})`);
  checks.push('Face menu Set as cover persists');
  await run("document.querySelector('#group-detail').close();location.hash='review-faces'");
  await wait(`document.querySelector('#review-groups [data-group="${unnamed}"]')`);
  await open(unnamed);
  group=(await api(`/groups/${unnamed}`)).group;
  const face=group.faces[0].detection_id;
  await textClick('#group-detail .group-album-tools button','Select');
  await click(`#group-detail [data-id="${face}"]`);
  await click('#group-detail .bulk-move-faces');
  await textClick('.person-destination-menu button','Browser renamed');
  await wait("document.querySelector('#group-detail .undo-correction:not([hidden]):not(:disabled)')");
  assert((await api(`/groups/${named}`)).group.faces.some(f=>f.detection_id===face));
  await click('#group-detail .undo-correction');
  await wait(`fetch('/api/people-preview/groups/${unnamed}').then(r=>r.json()).then(d=>d.group.faces.some(f=>f.detection_id===${face}))`);
  checks.push('Selected Move to person and Undo restore membership');
  await wait("document.querySelector('#group-detail .group-album-tools button')?.textContent==='Select'");
  await textClick('#group-detail .group-album-tools button','Select');
  await click(`#group-detail [data-id="${face}"]`);
  await click('#group-detail .bulk-ignore-faces');
  await wait(`fetch('/api/people-preview/ignored').then(r=>r.json()).then(d=>d.items.some(f=>f.detection_id===${face}))`);
  await run("document.querySelector('#group-detail').close();location.hash='ignored-faces'");
  await wait(`document.querySelector('.restore-ignored[data-face="${face}"]')`);
  await click(`.restore-ignored[data-face="${face}"]`);
  await wait(`!document.querySelector('.restore-ignored[data-face="${face}"]')`);
  assert(!(await api('/ignored')).items.some(f=>f.detection_id===face));
  checks.push('Bulk Ignore and Ignored faces Restore persist');
  await run("location.hash='review-faces'");
  await wait(`document.querySelector('#review-groups [data-group="${unnamed}"]')`);
  await open(unnamed);
  await wait("[...document.querySelectorAll('#group-detail img')].every(i=>i.complete&&i.naturalWidth>0)");
  const shot=await cdp('Page.captureScreenshot');
  writeFileSync(join(out,'people-detail.png'),Buffer.from(shot.data,'base64'));
  assert.deepEqual(await run('window.browserErrors'),[]);
  writeFileSync(join(out,'results.json'),JSON.stringify({ok:true,checks},null,2));
  console.log(JSON.stringify({ok:true,checks,artifacts:out}));
  await cdp('Browser.close');
} finally {ws?.close();chrome.kill();}
