import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import * as Vue from 'vue';
import {parse,compileScript} from '@vue/compiler-sfc';
import {renderToString} from 'vue/server-renderer';
import {dashboardTabs,collectionStatus} from './dashboardView.js';
import {priceCoveragePercent} from './priceCoverage.js';
const source=await readFile(new URL('./CollectionOverview.vue',import.meta.url),'utf8');
const {descriptor}=parse(source);
let body=compileScript(descriptor,{id:'overview-test',inlineTemplate:true}).content;
body=body.replace(/import \{([^}]+)\} from ["']vue["'];?/g,(_,names)=>`const {${names.replace(/\s+as\s+/g,': ')}}=Vue;`)
  .replace(/import .* from '\.\/.*';/g,'').replace('export default','return');
const component=new Function('Vue','collectionStatus','priceCoveragePercent',body)(Vue,collectionStatus,priceCoveragePercent);
test('four focused views replace the long dashboard',()=>{
  assert.deepEqual(dashboardTabs.map(t=>t.id),['overview','calendar','notifications','settings']);
});
test('disabled and blocked providers never appear healthy',()=>{
  assert.equal(collectionStatus({enabled:false,status:'ONLINE'}).label,'未启用');
  assert.equal(collectionStatus({enabled:true,status:'BLOCKED'}).tone,'warning');
});
test('overview shows configured transport and real coverage, not API success',async()=>{
  const html=await renderToString(Vue.createSSRApp(component,{
    names:{ihg:'IHG'}, providers:[{provider:'ihg',enabled:true,status:'ONLINE',collection_strategy:{label:'Cookie 浏览器',detail:'仅使用实际报价'}}],
    catalogs:[{provider:'ihg',hotels:10,hotels_with_quotes:3,price_coverage:{required_stays:3650,fresh_stays:1}}],
  }));
  assert.ok(html.includes('Cookie 浏览器')&&html.includes('&lt;0.1%'));
  assert.ok(html.includes('曾取得报价')&&!html.includes('100%'));
});
test('missing source metadata is unknown, not invented Cookie or API success',async()=>{
  const html=await renderToString(Vue.createSSRApp(component,{names:{gha:'GHA'},providers:[{provider:'gha'}]}));
  assert.ok(html.includes('采集方式待后台确认'));
});
test('advanced details and credentials are outside the default overview',async()=>{
  const app=await readFile(new URL('./App.vue',import.meta.url),'utf8');
  assert.ok(app.includes("const activeView=ref('overview')"));
  assert.ok(app.includes('<summary>高级采集设置</summary>'));
  assert.ok(app.includes('<summary>诊断与覆盖详情</summary>'));
  assert.equal((app.match(/<label>管理令牌/g)||[]).length,1);
  assert.ok(!app.includes('localStorage'));
});
