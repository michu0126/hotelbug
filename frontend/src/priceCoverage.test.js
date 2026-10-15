import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import * as Vue from 'vue';
import {parse,compileScript} from '@vue/compiler-sfc';
import {renderToString} from 'vue/server-renderer';
import * as helpers from './priceCoverage.js';
import {countLabel,priceCoveragePercent} from './priceCoverage.js';

test('missing coverage does not imply complete or zero coverage',()=>{
  assert.equal(priceCoveragePercent(null),'—');
  assert.equal(priceCoveragePercent({required_stays:0,fresh_stays:0}),'—');
  assert.equal(countLabel(undefined),'—');
  assert.equal(countLabel(null),'—');
  assert.equal(countLabel(-1),'—');
  assert.equal(countLabel(0),'0');
});

const source=await readFile(new URL('./PriceCoverage.vue',import.meta.url),'utf8');
const {descriptor}=parse(source);
const script=compileScript(descriptor,{id:'price-coverage-test',inlineTemplate:true}).content;
const body=script.replace(/import \{([^}]+)\} from ["']vue["'];?/g,(_,names)=>
  `const {${names.replace(/\s+as\s+/g,': ')}}=Vue;`
).replace(/import \{([^}]+)\} from ["']\.\/priceCoverage.js["'];?/g,(_,names)=>
  `const {${names}}=helpers;`
).replace('export default','return');
const component=new Function('Vue','helpers',body)(Vue,helpers);

test('actual panel distinguishes dated freshness from catalog and queue counts',async()=>{
  const html=await renderToString(Vue.createSSRApp(component,{names:{marriott:'万豪'},catalogs:[{
    provider:'marriott',price_coverage:{eligible_hotels:2,required_stays:730,fresh_stays:1,
      stale_stays:4,unscanned_stays:725,fresh_available_stays:1,fresh_unavailable_stays:0,
      fresh_not_open_stays:0,source_unverified_stays:2,refresh_demand_exceeds_start_ceiling:true}
  }]}));
  assert.ok(html.includes('1 / 730'));
  assert.ok(html.includes('不是全球酒店总量'));
  assert.ok(html.includes('旧来源或来源待核对 2'));
  assert.ok(html.includes('需要多日批量采集能力'));
  assert.ok(!html.includes('NaN'));
});

test('legacy backend is explicitly unknown rather than complete',async()=>{
  const html=await renderToString(Vue.createSSRApp(component,{names:{marriott:'万豪'},catalogs:[{provider:'marriott'}]}));
  assert.ok(html.includes('尚未提供真实日期覆盖统计'));
  assert.ok(!html.includes('100%'));
});
test('only fresh observed stays count toward coverage',()=>{
  assert.equal(priceCoveragePercent({required_stays:365,fresh_stays:0,previously_scanned_stays:365}),'0%');
  assert.equal(priceCoveragePercent({required_stays:365,fresh_stays:365}),'100%');
  assert.equal(priceCoveragePercent({required_stays:10000,fresh_stays:9999}),'99.9%');
  assert.equal(priceCoveragePercent({required_stays:10000,fresh_stays:1}),'<0.1%');
  assert.equal(priceCoveragePercent({required_stays:365,fresh_stays:366}),'—');
});

test('calendar hints are rendered separately without increasing room coverage',async()=>{
  const html=await renderToString(Vue.createSSRApp(component,{names:{marriott:'万豪'},catalogs:[{
    provider:'marriott',price_coverage:{eligible_hotels:2,required_stays:730,fresh_stays:0,
      stale_stays:0,unscanned_stays:730,fresh_available_stays:0,fresh_unavailable_stays:0,fresh_not_open_stays:0},
    calendar_hints:{supported:true,eligible_hotels:2,observed_hotels:1,fresh_dates:30,observed_dates:48,stale_dates:18}
  }]}));
  assert.ok(html.includes('0 / 730'));
  assert.ok(html.includes('30 / 48'));
  assert.ok(html.includes('1 / 2'));
  assert.ok(html.includes('未计入房型覆盖'));
  assert.ok(html.includes('税费是否包含、是否要求会员均未确认'));
  assert.ok(!html.includes('当前采集器逐日查价'));
});

test('unsupported or unknown calendar source is not displayed as measured zero',async()=>{
  const html=await renderToString(Vue.createSSRApp(component,{names:{marriott:'万豪',ihg:'IHG'},catalogs:[
    {provider:'marriott',calendar_hints:{supported:false,status:'CN_SOURCE_NOT_SELECTED'}},
    {provider:'ihg',calendar_hints:{supported:false,status:'UNSUPPORTED_PROVIDER'}}
  ]}));
  assert.ok(html.includes('尚未采纳万豪中文官网来源'));
  assert.ok(html.includes('该集团暂未接入按月日历起价'));
  assert.ok(!html.includes('0 / 0'));
  assert.ok(!html.includes('100%'));
});
