const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('static/app.js', 'utf8');
class Element {
  constructor(tag, cls='', text='') { this.tag=tag; this.className=cls; this._text=text; this.children=[]; this.dataset={}; this.style={}; this.attributes={};
    this.classList={add:n=>{this.className+=' '+n;},toggle:(n,on)=>{const names=new Set(this.className.split(' '));on?names.add(n):names.delete(n);this.className=[...names].join(' ');}};
  }
  appendChild(n) { this.children.push(n); return n; }
  replaceChildren(...nodes) { this.children=nodes;this._text=''; }
  get textContent() { return this._text + this.children.map(n=>n.textContent).join(' '); }
  set textContent(t) { this.children=[];this._text=t; }
  set innerHTML(t) { this.replaceChildren(); }
  setAttribute(k,v) { this.attributes[k]=v; }
  querySelectorAll(selector) { return all(this).filter(e=>e.className.split(" ").includes(selector.slice(1))); }
}
const all = n=>[n,...n.children.flatMap(all)];
const byClass=(n,c)=>all(n).filter(e=>e.className.split(' ').includes(c));
let status={};let posts=[];const nodes={'#setup-body':new Element('div'),'#setup-mode':new Element('div'),'#companion-body':new Element('div')};
for(const id of ['#wa-status','#wa-hint','#wa-qr-box','#wa-connect','#wa-qr'])nodes[id]=new Element('div');
const context=vm.createContext({el:(...args)=>new Element(...args),$:id=>nodes[id]||null,
  setupSt:null,setupActive:null,setupExtra:{},REDUCED_MOTION:true,
  api:async url=>url.endsWith('/qr')?{png:'data:image/png;base64,fixture'}:status,
  post:async(url,body)=>{posts.push({url,body});return {};},errText:e=>e.message,
  setTimeout:()=>{},startPoll:()=>({stop(){}}),stopWaPoll:()=>{},companionPollT:null,
  cardDisk:()=>{},cardContacts:()=>{},cardDossiers:()=>{},cardBrain:()=>{},cardMail:()=>{},cardBanking:()=>{},
  backendBlock:()=>{},provCard:()=>{},cardNotifications:()=>{},cardUpdates:()=>{},openFirstrun:()=>{},
  connectionSection:(host,title,text,render)=>{const d=new Element('details','',title);d.appendChild(new Element('p','',text));render(d);host.appendChild(d);},
  loadCompanion:()=>Promise.resolve(),companionPairStart:()=>{},copyText:()=>{},toast:()=>{},
  window:{open(){}},URL,fmtTime:()=>'',confirm:()=>false,del:()=>Promise.resolve(),
});
function section(first,last){const a=app.indexOf(first),b=app.indexOf(last,a);assert(a>=0&&b>a);return app.slice(a,b);}
vm.runInContext(section('const DASH_NOUNS =','// ---- step cards'),context);
vm.runInContext(section('const SETUP_MANAGE =','function cardNotifications('),context);
vm.runInContext(section('function renderCompanion(','async function companionPairStart('),context);
vm.runInContext(section('let waPoll;','// Backend + default models,'),context);
const providers=[{id:'anthropic',sub_name:'Claude',connected:true,detail:'Signed in'}, {id:'gemini',sub_name:'Gemini',connected:false,detail:'Not installed'}];
const flow={complete:true,done:6,total:6,steps:[{id:'ai',providers,active_id:'anthropic'},...['disk','contacts','dossiers','brain','mail'].map(id=>({id,title:id,state:'done',detail:'Connected'}))]};
const state={platform:'mac',feed:{chat_db:'ok'},crm:{people:2,profiles:1},vault:{connected:true,notes:3},mail:{accounts:1}};
context.setupExtra={companion:{devices:[],hub_url:'http://example-host:8377'},notify:{enabled:true,handle:''},update:{git:true,behind:0},config:{ai_backend:'cli'},whatsapp:{linked:false,installed:false}};
context.renderSetup(flow,state);
const titles=byClass(nodes['#setup-body'],'setup-step-title').map(n=>n.textContent);
assert(titles.includes('Advanced AI settings'));
assert(titles.includes('Profiles'));
assert(!titles.includes('Dossiers'));
assert(!titles.includes('Storage & connections'));
assert(!byClass(nodes['#setup-body'],'dash-group-title').some(n=>n.textContent==='Channels'));
assert.equal(byClass(nodes['#setup-body'],'dash-facts').length,0,'counts are not repeated in a second navigation strip');
assert.equal(nodes['#setup-mode'].textContent,'','readiness is stated once');
let phone=byClass(nodes['#setup-body'],'dash-item').find(n=>n.dataset.setupId==='channels');
assert.match(phone.children[0].className,/s-done/);
assert.match(phone.textContent,/Your iPhone messages can be read/);
let optional=byClass(nodes['#setup-body'],'dash-item').find(n=>n.dataset.setupId==='whatsapp');
assert.match(optional.children[0].className,/s-optional/);
assert.match(optional.textContent,/optional/i);
let notifications=byClass(nodes['#setup-body'],'dash-item').find(n=>n.dataset.setupId==='notifications');
assert.match(notifications.children[0].className,/s-optional/,'enabled flag alone cannot claim notifications are configured');
let banking=byClass(nodes['#setup-body'],'dash-item').find(n=>n.dataset.setupId==='banking');
assert.match(banking.children[0].className,/s-optional/);
context.setupExtra.banking={mercury:{configured:true}};
context.renderSetup(flow,state);
banking=byClass(nodes['#setup-body'],'dash-item').find(n=>n.dataset.setupId==='banking');
assert.match(banking.children[0].className,/s-done/);
assert.match(banking.textContent,/Mercury token configured/);
const phoneCard=new Element('div');context.cardChannels(phoneCard);
assert.match(phoneCard.textContent,/No phone pairing is needed/);
assert.match(phoneCard.textContent,/Android phone \(optional\)/);
assert.match(phoneCard.textContent,/Tailscale/);
assert(!phoneCard.textContent.includes('WhatsApp'),'independent source is not duplicated inside the phone card');
context.setupExtra.companion.hub_url='http://192.168.1.20:8377';
const localPhoneCard=new Element('div');context.cardChannels(localPhoneCard);
assert.match(localPhoneCard.textContent,/Same Wi-Fi address/);
assert.match(localPhoneCard.textContent,/reopen this card to get your Tailscale address/);
context.renderCompanion({devices:[]});
assert(!nodes['#companion-body'].textContent.includes('No phone'));
assert.match(nodes['#companion-body'].textContent,/For Android SMS/);
context.renderSetup(flow,{...state,platform:'win',feed:{chat_db:'missing'}});
assert.equal(context.manageState('channels'),'optional');
context.setupExtra.companion.devices=[{pending:false}];
assert.equal(context.manageState('channels'),'done');
context.setupExtra.whatsapp={sidecar:{connected:true}};
assert.equal(context.manageState('whatsapp'),'done');
context.setupExtra.whatsapp={pairing:{error:'Connection failed'}};
assert(context.manageAttention('whatsapp'));
(async()=>{
  status={installed:false,linked:false,pairing:{running:true,stage:'preparing'}};
  await context.waTick();assert(nodes['#wa-connect'].disabled);assert.match(nodes['#wa-hint'].textContent,/preparing WhatsApp/);
  status={installed:false,linked:false,pairing:{running:false,error:'Install failed'}};
  await context.waTick();assert(!nodes['#wa-connect'].disabled);assert.equal(nodes['#wa-hint'].textContent,'Install failed');
  await context.waConnect();assert.equal(posts.at(-1).url,'/api/whatsapp/pair');
  status={installed:true,linked:false,sidecar:{connected:false},pairing:{running:false,stage:'ready'}};
  await context.waTick();assert.equal(nodes['#wa-qr'].src,'data:image/png;base64,fixture');assert.equal(nodes['#wa-qr-box'].style.display,'block');
  assert.match(nodes['#wa-hint'].textContent,/Linked Devices/);
  status={installed:true,linked:true,sidecar:{connected:true}};
  await context.waTick();assert.equal(nodes['#wa-qr-box'].style.display,'none');assert.equal(nodes['#wa-connect'].style.display,'none');
  context.api=async()=>({ai_backend:'cli',api_key_present:false,api_key_env:'EXAMPLE_API_KEY'});
  context.modelCatalog=async()=>({providers:[{id:'fixture',label:'Example',connected:true,config_keys:{cli:'cli_model',api:'api_model'},cli:[],api:[],cli_detail:'Ready',api_detail:'Optional API'}]});
  context.fillModelSelect=select=>select;context.rosterBlock=()=>{};
  vm.runInContext(section('function backendBlock(', '// ---- the model roster'),context);
  const backend=new Element('div');context.backendBlock(backend);
  const save=all(backend).find(n=>n.tag==='button'&&n.textContent==='Save');
  assert(save.disabled,'settings cannot be saved before current values load');
  await new Promise(resolve=>setImmediate(resolve));
  assert(!save.disabled);
  const apiField=byClass(backend,'setup-api-model')[0];
  assert(byClass(backend,'setup-api-model').every(n=>n.hidden),'API explanations are also folded away for subscription users');
  const keyHint=all(backend).find(n=>n.id==='cfg-api-hint');
  assert(apiField.hidden,'API settings do not imply missing work for a subscription login');
  assert(keyHint.hidden,'API-key warning is hidden for subscription users');
  const seg=all(backend).find(n=>n.id==='backend-seg');seg.children[1].onclick();
  assert(!apiField.hidden);assert(!keyHint.hidden,'API users still get honest credential guidance');
  context.modelCatalog=async()=>({error:'Catalog unavailable'});
  const failedBackend=new Element('div');context.backendBlock(failedBackend);
  await new Promise(resolve=>setImmediate(resolve));
  const failedSave=all(failedBackend).find(n=>n.tag==='button'&&n.textContent==='Save');
  assert(failedSave.disabled);
  assert.match(failedBackend.textContent,/AI settings unavailable: Catalog unavailable/);
  const retry=all(failedBackend).find(n=>n.textContent==='Retry loading settings');
  assert(!retry.hidden);
  context.modelCatalog=async()=>({providers:[]});
  await retry.onclick();
  assert(!failedSave.disabled);assert(retry.hidden);
  console.log('Config states, phone identity, optional groups, AI defaults and WhatsApp progress passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
