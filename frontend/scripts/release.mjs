// Version the static-shell cache from the actual built assets, not a manual counter.
import {readFileSync,writeFileSync,readdirSync} from 'node:fs';
import {createHash} from 'node:crypto';
const hash=createHash('sha256');
for(const name of readdirSync(new URL('../dist/assets/',import.meta.url)).sort())hash.update(name);
hash.update(readFileSync(new URL('../dist/index.html',import.meta.url)));
const target=new URL('../dist/sw.js',import.meta.url);
writeFileSync(target,readFileSync(target,'utf8').replace("PREFIX+'v3'","PREFIX+'"+hash.digest('hex').slice(0,20)+"'"));
