import {build} from 'esbuild';
import {writeFile} from 'node:fs/promises';
const result=await build({entryPoints:['src/index.ts'],outfile:'dist/index.js',bundle:true,platform:'node',format:'esm',target:'node24',external:['openclaw/*'],metafile:true});
for(const output of Object.values(result.metafile.outputs)) for(const entry of output.imports) {
 if(!entry.external || !(entry.path.startsWith('node:') || entry.path.startsWith('openclaw/'))) throw new Error('Unexpected runtime import: '+entry.path);
}
await writeFile('dist/bundle-meta.json',JSON.stringify(result.metafile,null,2)+'\n');
