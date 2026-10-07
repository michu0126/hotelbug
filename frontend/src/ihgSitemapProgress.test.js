import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import * as Vue from 'vue';
import {parse,compileScript} from '@vue/compiler-sfc';
import {renderToString} from 'vue/server-renderer';

const source=await readFile(new URL('./IhgSitemapProgress.vue',import.meta.url),'utf8');
const {descriptor}=parse(source);
const script=compileScript(descriptor,{id:'ihg-sitemap-test',inlineTemplate:true}).content;
// Compile our own SFC to exercise its actual template/props, not a duplicate
// formatting function. Vue imports become local bindings in this test only.
const body=script.replace(/import \{([^}]+)\} from ["']vue["'];?/g,(_,names)=>
  `const {${names.replace(/\s+as\s+/g,': ')}}=Vue;`
).replace('export default','return');
const component=new Function('Vue',body)(Vue);
const progress={sitemaps_read:1,sitemaps_total:26,sitemaps_missing:0,sitemaps_failed:2,
  hotel_candidates:61,candidate_positions_processed:1,last_error:null};

test('IHG supplementary panel stays hidden before there is authoritative source state',async()=>{
  const html=await renderToString(Vue.createSSRApp(component,{progress:null}));
  assert.ok(!html.includes('IHG 官网地图补采'));
  assert.ok(!html.includes('去重酒店候选'));
});

test('actual panel separates candidate positions from hotels and year quote coverage',async()=>{
  const html=await renderToString(Vue.createSSRApp(component,{progress}));
  assert.ok(html.includes('已读地图 1 / 26'));
  assert.ok(html.includes('去重酒店候选 61'));
  assert.ok(html.includes('已经过候选位置 1'));
  assert.ok(html.includes('候选位置不是成功收录数，也不是全年报价覆盖数'));
  assert.ok(!html.includes('地图问题：'));
});

test('source errors are displayed as escaped text, not executable markup',async()=>{
  const html=await renderToString(Vue.createSSRApp(component,{progress:{...progress,last_error:'<script>alert(1)</script>'}}));
  assert.ok(html.includes('地图问题：&lt;script&gt;alert(1)&lt;/script&gt;'));
  assert.ok(!html.includes('<script>'));
});
