import {spawnSync} from 'node:child_process';
import {mkdirSync,writeFileSync,readFileSync,statSync,mkdtempSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
const logs='delivery-logs/m4';mkdirSync(logs,{recursive:true});
const state=mkdtempSync(join(tmpdir(),'fact-m4-cli-'));
const env={...process.env,OPENCLAW_STATE_DIR:join(state,'state'),OPENCLAW_CONFIG_PATH:join(state,'absent-config.json')};
const commands=[['build','npm',['run','build']],['bundle','npm',['run','plugin:build']],['metadata','openclaw',['plugins','build','--entry','./dist/index.js']],['check','openclaw',['plugins','build','--check','--entry','./dist/index.js']],['validate','openclaw',['plugins','validate','--entry','./dist/index.js']],['tests','npm',['test']],['pack','npm',['pack','--dry-run','--json']],['host','node',['scripts/verify-m4-host.mjs',process.argv[2]??'/app']]];
const results=[];
if(!process.argv.includes('--audit-only')) {
for(const [label,cmd,args] of commands){
 const r=spawnSync(cmd,args,{env,encoding:'utf8',maxBuffer:20*1024*1024});
 writeFileSync(`${logs}/${label}.txt`,r.stdout+r.stderr);
 results.push({command:[cmd,...args],exitCode:r.status,signal:r.signal,error:r.error?.message});
 console.log(label,r.status);
}
writeFileSync(`${logs}/results.json`,JSON.stringify({state,results},null,2)+'\n');
assert(results.every(r=>r.exitCode===0),'Command failed; inspect logs');
}
const packed=JSON.parse(readFileSync(`${logs}/pack.txt`,'utf8'));
const pack=Array.isArray(packed)?packed[0]:Object.values(packed)[0];
assert.deepEqual(pack.files.map(f=>f.path).sort(),['README.md','dist/index.js','openclaw.plugin.json','package.json'].sort());
const meta=JSON.parse(readFileSync('dist/bundle-meta.json','utf8'));
const imports=meta.outputs['dist/index.js'].imports;
assert(imports.every(i=>i.external&&(i.path.startsWith('node:')||i.path.startsWith('openclaw/'))));
const audit={hostPeerRequires:['openclaw/plugin-sdk/tool-plugin'],bundleBytes:statSync('dist/index.js').size,imports,packageFiles:pack.files,dependencies:JSON.parse(readFileSync('package.json','utf8')).dependencies??{}};
writeFileSync(`${logs}/audit.json`,JSON.stringify(audit,null,2)+'\n');
console.log(JSON.stringify(audit,null,2));
