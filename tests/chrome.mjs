// Locates a Chrome/Chromium for the *-browser.mjs scripts.
// FAMILYFOTO_CHROME overrides discovery. Run `node tests/chrome.mjs` to print the path (exit 1 if none).
import {existsSync} from 'node:fs';
import {delimiter,join} from 'node:path';
import {fileURLToPath} from 'node:url';

const WINDOWS=[
 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
 'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
];
const MAC=[
 '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
 '/Applications/Chromium.app/Contents/MacOS/Chromium',
];
const LINUX=['google-chrome','google-chrome-stable','chromium','chromium-browser'];

function onPath(names){
 for(const dir of (process.env.PATH||'').split(delimiter))
  for(const name of names){const path=join(dir,name);if(dir&&existsSync(path))return path;}
 return null;
}

export function findChrome(){
 const override=process.env.FAMILYFOTO_CHROME;
 if(override)return existsSync(override)?override:null;
 if(process.platform==='win32')return WINDOWS.find(existsSync)??null;
 if(process.platform==='darwin')return MAC.find(existsSync)??null;
 return onPath(LINUX);
}

export function chromePath(){
 const path=findChrome();
 if(!path)throw new Error('Chrome not found; install it or set FAMILYFOTO_CHROME to its executable');
 return path;
}

if(process.argv[1]===fileURLToPath(import.meta.url)){
 const path=findChrome();
 if(path){console.log(path);}else{process.exitCode=1;}
}
