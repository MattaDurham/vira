// Exercise the real skin apply client, including the persistence/reload join.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('static/app.js','utf8');
const functionText=source.slice(source.indexOf('async function doApplySkin(s)'),source.indexOf('// ----- vault note focus panel'));
const storage=new Map(),calls=[],timers=[],messages=[];
let preset=null,finishSave;
const sandbox={toast:message=>messages.push(message),setTimeout:fn=>timers.push(fn),location:{reload:()=>calls.push('reload')},
  lsSet:(key,value)=>{const raw=JSON.stringify(value);storage.set(key,raw);return raw;},
  post:async(url,body)=>{
    calls.push({url,body});
    if(url.endsWith('/apply'))return {ok:true,background:preset};
    await new Promise(resolve=>{finishSave=resolve;});return {ok:true};
  }};
vm.runInNewContext(functionText+'\nthis.apply=doApplySkin;',sandbox);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  for(const id of ['living-garden','neon-pond']){
    preset=JSON.parse(fs.readFileSync('static/skins/'+id+'.json','utf8')).background;
    const before=timers.length,work=sandbox.apply({id,name:id});await flush();
    assert.equal(timers.length,before,'reload must wait for server persistence, not just localStorage');
    const save=calls.at(-1);
    assert.equal(save.url,'/api/ui-state');assert.equal(save.body.keys['vira-background'],storage.get('vira-background'));
    assert.deepEqual(JSON.parse(storage.get('vira-background')),preset,'the skin carries its actual scene and material look');
    finishSave();await work;assert.equal(timers.length,before+1);timers.at(-1)();assert.equal(calls.at(-1),'reload');
  }
  const previous=storage.get('vira-background'),requests=calls.length;preset=null;
  await sandbox.apply({id:'darkmode',name:'Dark Mode'});
  assert.equal(calls.length,requests+1,'a skin without a scene leaves the existing background choice intact');
  assert.equal(storage.get('vira-background'),previous);
  console.log('Skin scene presets persist to both UI stores before reload.');
})().catch(error=>{console.error(error);process.exitCode=1;});
