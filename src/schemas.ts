export type Json=null|boolean|number|string|Json[]|{[k:string]:Json};
export type ToolName='fact_create'|'fact_read'|'fact_plan'|'fact_record'|'fact_finish';
export type JsonSchema=Record<string,any>;
export class FactError extends Error {constructor(public tag:string,message=tag,public details:Json={},options?:ErrorOptions){super(message,options);}}
export interface Config {workRoot:string;projectsRoot:string;workDir:string;indexPath:string;staleDays:number;sqliteTimeoutMs:number}
export type Success={ok:true;data:Json;revision?:number;[k:string]:Json|undefined};
export type Failure={ok:false;error:{code:string;message:string;details:Json;retryable:boolean}};
export type Response=Success|Failure;
export type CallContext={toolCallId?:string;signal?:AbortSignal};
export type CreateArgs={action:'create';slug:string;purpose:string;kind:'project'|'light';request_id:string;name?:string;root_path?:string};
const fail=(m:string):never=>{throw new FactError('VALIDATION_ERROR',m);};
const cpCompare=(a:string,b:string)=>{const x=Array.from(a,c=>c.codePointAt(0)!),y=Array.from(b,c=>c.codePointAt(0)!);for(let i=0;i<Math.min(x.length,y.length);i++)if(x[i]!==y[i])return x[i]-y[i];return x.length-y.length;};
function validString(s:string){for(const c of s){const n=c.codePointAt(0)!;if(n>=0xd800&&n<=0xdfff)fail('lone surrogate');}}
function serialize(v:Json,spaces:boolean,ascii=false):string {
 const quote=(s:string)=>{let q=JSON.stringify(s);if(ascii)q=q.replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));return q;};
 if(v===null||typeof v==='boolean'||typeof v==='number')return JSON.stringify(v);
 if(typeof v==='string')return quote(v);
 if(Array.isArray(v))return '['+v.map(x=>serialize(x,spaces,ascii)).join(spaces?', ':',')+']';
 return '{'+Object.keys(v).sort(cpCompare).map(k=>quote(k)+(spaces?': ':':')+serialize(v[k],spaces,ascii)).join(spaces?', ':',')+'}';
}
function validateJson(value:unknown,bounded:boolean):asserts value is Json {
 let nodes=0;const active=new Set<object>();
 function visit(v:unknown,d:number):void {if(bounded&&(d>12||++nodes>5000))fail('JSON depth/node limit');if(v===null||typeof v==='boolean')return;
 if(typeof v==='string'){validString(v);if(bounded&&Array.from(v).length>200000)fail('string limit');return;}
 if(typeof v==='number'){if(!Number.isFinite(v)||(Number.isInteger(v)&&!Number.isSafeInteger(v)))fail('number outside supported domain');return;}
 if(typeof v!=='object'||!v||(!Array.isArray(v)&&Object.getPrototypeOf(v)!==Object.prototype))return fail('ordinary JSON required');
 if(active.has(v))fail('cyclic JSON');active.add(v);
 if(Array.isArray(v)){for(let i=0;i<v.length;i++){if(!Object.hasOwn(v,i))fail('sparse array');visit(v[i],d+1);}}else{if(Reflect.ownKeys(v).length!==Object.keys(v).length)fail('non JSON properties');for(const [k,x] of Object.entries(v)){validString(k);visit(x,d+1);}}active.delete(v);}
 visit(value,0);if(bounded&&Buffer.byteLength(serialize(value as Json,true),'utf8')>1000000)fail('JSON byte limit');
}
export function validateJsonBounds(value:unknown):asserts value is Json {validateJson(value,true);}
// Internal persistence/digests validate JSON structure, never public input quotas.
export function internalJson(value:Json,spaces=false):string{validateJson(value,false);return serialize(value,spaces);}
export function canonicalJson(value:Json):string{return internalJson(value);}
export function pythonDefaultJson(value:Json):string{return internalJson(value,true);}
const keywords=new Set('type properties additionalProperties required enum const minLength maxLength minimum maximum minItems maxItems maxProperties items allOf if then not anyOf oneOf'.split(' '));
function checkSchema(s:JsonSchema):void {for(const k of Object.keys(s))if(!keywords.has(k))throw new Error('Unsupported schema keyword: '+k);for(const q of Object.values(s.properties??{}))checkSchema(q as JsonSchema);for(const k of ['items','if','then','not'])if(s[k])checkSchema(s[k]);if(typeof s.additionalProperties==='object')checkSchema(s.additionalProperties);for(const k of ['allOf','anyOf','oneOf'])for(const q of s[k]??[])checkSchema(q);}
export function validateSchema(value:unknown,schema:JsonSchema,path='$'):void {
 checkSchema(schema);
 function test(v:any,s:JsonSchema,p:string):void {
 const bad=(m:string)=>fail(p+': '+m);const matches=(q:JsonSchema)=>{try{test(v,q,p);return true;}catch(e){if(e instanceof FactError)return false;throw e;}};
 if(s.type){const ts=Array.isArray(s.type)?s.type:[s.type];if(!ts.some((t:string)=>t==='null'?v===null:t==='array'?Array.isArray(v):t==='object'?v!==null&&typeof v==='object'&&!Array.isArray(v):t==='integer'?typeof v==='number'&&Number.isSafeInteger(v):t==='number'?typeof v==='number'&&Number.isFinite(v):typeof v===t))bad('type');}
 if(s.enum&&!s.enum.some((x:any)=>JSON.stringify(x)===JSON.stringify(v)))bad('enum');if(Object.hasOwn(s,'const')&&JSON.stringify(s.const)!==JSON.stringify(v))bad('const');
 if(typeof v==='string'){const n=Array.from(v).length;if(n<(s.minLength??0)||n>(s.maxLength??Infinity))bad('string length');}
 if(typeof v==='number'&&(v<(s.minimum??-Infinity)||v>(s.maximum??Infinity)))bad('number range');
 if(Array.isArray(v)){if(v.length<(s.minItems??0)||v.length>(s.maxItems??Infinity))bad('array length');if(s.items)v.forEach((x,i)=>test(x,s.items,p+'['+i+']'));}
 if(v!==null&&typeof v==='object'&&!Array.isArray(v)){for(const k of s.required??[])if(!Object.hasOwn(v,k))bad('required '+k);if(Object.keys(v).length>(s.maxProperties??Infinity))bad('property count');for(const [k,x] of Object.entries(v)){if(Object.hasOwn(s.properties??{},k))test(x,s.properties[k],p+'.'+k);else if(s.additionalProperties===false)bad(k==='item_ref'?'item_ref removed; use item_id':'unknown field '+k);else if(typeof s.additionalProperties==='object')test(x,s.additionalProperties,p+'.'+k);}}
 for(const q of s.allOf??[])test(v,q,p);if(s.if&&matches(s.if)&&s.then)test(v,s.then,p);if(s.not&&matches(s.not))bad('forbidden fields');if(s.anyOf&&!s.anyOf.some(matches))bad('anyOf');if(s.oneOf&&s.oneOf.filter(matches).length!==1)bad('oneOf');
 }test(value,schema,path);
}
const str=(max=10000,min=1):JsonSchema=>({type:'string',minLength:min,maxLength:max});
const en=(...xs:string[]):JsonSchema=>({type:'string',enum:xs});
const obj=(properties:Record<string,JsonSchema>,required:string[]=[]):JsonSchema=>({type:'object',properties,required,additionalProperties:false});
const arr=(items:JsonSchema,min=0):JsonSchema=>({type:'array',items,minItems:min,maxItems:100});
const strings=(keys:string)=>Object.fromEntries(keys.split(' ').map(k=>[k,str()]));
const link=obj({item_id:str(),relation:en('context','affects','evidence')});
const relation=obj({target_id:str(),target_ref:str(),relation:en('supports','contradicts','evidence_for','follows')});
const file=obj({...strings('locator purpose corpus_id source_locator item_id'),storage:en('local','corpus','external'),role:en('temp','artifact','reference'),handoff_status:en('not_applicable','pending_handoff','accepted','failed')},['locator','storage','role','purpose']);
const shared={links:arr(link),relations:arr(relation),files:arr(file)};
const record=obj({...strings('kind title body ref status supersedes_id supersedes_ref hypothesis workaround resolution issue_key summary rationale decision_key expected_result actual_result run_at result evidence'),...shared,severity:en('low','medium','high','critical'),verdict:en('pass','fail','inconclusive'),input_config:{type:['object','string']}},['kind','title','body']);
const change=obj({...strings('op ref name purpose parent_id parent_ref item_id item_ref requires_id requires_ref title description'),kind:en('goal','task','project'),position:{type:'integer',minimum:0},acknowledge_migration_review:{type:'boolean'}},['op']);
function tool(properties:Record<string,JsonSchema>,rules:[string,string[],string[]][]):JsonSchema{return {...obj({action:en(...rules.map(r=>r[0])),...properties},['action']),allOf:rules.map(([action,required,forbidden])=>({if:{properties:{action:{const:action}},required:['action']},then:{required,...(forbidden.length?{not:{anyOf:forbidden.map(k=>({required:[k]}))}}:{})}}))};}
const ids=Object.fromEntries('work request_id proposal_id id cursor record_id item_id target_id'.split(' ').map(k=>[k,str(256)]));
const confirmation=obj({summary:str(2000)},['summary']);
const schemas:Record<ToolName,JsonSchema>={
 fact_create:tool({slug:str(100),purpose:str(),kind:en('project','light'),request_id:ids.request_id,name:str(500),root_path:str(4096)},[['create',['slug','purpose','kind','request_id'],[]]]),
 fact_read:tool({work:ids.work,id:ids.id,cursor:ids.cursor,collection:en('items','records','files','proposals'),limit:{type:'integer',minimum:1,maximum:200}},[['overview',[],['work','id','collection','limit','cursor']],['current',['work'],['id','collection','cursor']],['detail',['work'],[]],['history',['work'],['id','collection']]]),
 fact_plan:tool({work:ids.work,request_id:ids.request_id,proposal_id:ids.proposal_id,confirmation,changes:arr(change,1),review_summary:str()},[['propose',['work','request_id','changes'],['proposal_id','confirmation']],['apply',['work','request_id','proposal_id','confirmation'],['changes','review_summary']],['reject',['work','request_id','proposal_id'],['changes','review_summary']]]),
 fact_record:tool({work:ids.work,request_id:ids.request_id,record_id:ids.record_id,item_id:ids.item_id,records:arr(record,1),...strings('title status resolution reason'),body:str(200000,0),payload:{type:'object',maxProperties:100},...shared,expected_version:{type:'integer',minimum:1}},[['append',['work','request_id','records'],'record_id item_id title body payload links relations files status resolution reason expected_version'.split(' ')],['update',['work','request_id','record_id','expected_version'],['records','item_id','reason']],...['start','block'].map(a=>[a,['work','request_id','item_id','expected_version'],'records record_id title body payload links relations files status resolution'.split(' ')] as [string,string[],string[]])]),
 fact_finish:tool({work:ids.work,request_id:ids.request_id,proposal_id:ids.proposal_id,target_id:ids.target_id,target_type:en('work','goal','task'),mode:en('complete','archive','restore'),confirmation},[['prepare',['work','request_id','target_type','target_id'],['proposal_id','confirmation']],['confirm',['work','request_id','proposal_id','confirmation'],['mode','target_type','target_id']],['continue',['work','request_id','proposal_id'],['mode','target_type','target_id','confirmation']]])
};
schemas.fact_read.allOf.push({if:{properties:{action:{const:'detail'}},required:['action']},then:{oneOf:[{required:['id']},{required:['collection']}]}});
for(const s of Object.values(schemas))checkSchema(s);
export function toolSchemas():Readonly<Record<ToolName,JsonSchema>>{return schemas;}
export function validatePublic(name:ToolName,raw:unknown):Record<string,any>{validateJsonBounds(raw);validateSchema(raw,schemas[name]);const a=raw as Record<string,any>;if(name==='fact_read'&&a.action==='detail'&&a.id&&('limit'in a||'cursor'in a))fail('entity detail forbids limit/cursor');if(name==='fact_create'&&!/^[a-z0-9_-]+$/.test(a.slug))fail('invalid slug');return a;}
export const configSchema=obj({workRoot:str(4096),indexPath:str(4096),staleDays:{type:'integer',minimum:0},sqliteTimeoutMs:{type:'integer',minimum:0}},['workRoot','indexPath']);
export function parseConfig(value:unknown):Config{validateJsonBounds(value);validateSchema(value,configSchema);const v=value as Record<string,any>;return {...v,projectsRoot:v.workRoot+'/projects',workDir:v.workRoot+'/work',staleDays:v.staleDays??14,sqliteTimeoutMs:v.sqliteTimeoutMs??30000} as Config;}
