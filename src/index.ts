import {createRequire} from 'node:module';
import {configSchema,parseConfig,toolSchemas,type ToolName} from './schemas.js';
import {executeTool} from './service.js';
// The host owns this SDK; it is not a runtime package dependency.
const {defineToolPlugin}=createRequire(import.meta.url)('openclaw/plugin-sdk/tool-plugin');
const descriptions:Record<ToolName,string>={fact_create:'Create a work with a durable receipt',fact_read:'Read overview, current state, detail, or history',fact_plan:'Propose, apply, or reject a plan',fact_record:'Append or update records; start or block tasks',fact_finish:'Prepare, confirm, or continue completion'};
export default defineToolPlugin({id:'fact-infra',name:'Fact Infrastructure',description:'Fact infrastructure v3',configSchema,
 tools:(tool:any)=>Object.entries(toolSchemas()).map(([name,parameters])=>tool({name,label:name,description:descriptions[name as ToolName],parameters,execute:(params:unknown,config:unknown,ctx:any)=>executeTool(name as ToolName,params,parseConfig(config),ctx)}))});
