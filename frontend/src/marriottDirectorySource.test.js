import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import * as Vue from 'vue';
import {parse,compileScript} from '@vue/compiler-sfc';
import {renderToString} from 'vue/server-renderer';
import * as helpers from './marriottDirectorySource.js';

const choices=[{label:'中文官网（全球目录）',url:'https://www.marriott.com.cn/hotel-search.mi'},
  {label:'英文官网（全球目录）',url:'https://www.marriott.com/hotel-search.mi'}];
const catalog={provider:'marriott',enabled:true,directory_root_choices:choices,
  selected_directory_root:choices[1].url,requested_directory_root:null,requested_directory_root_status:null};

test('source choice generates the existing global directory job rather than individual hotel jobs',()=>{
  assert.deepEqual(helpers.directorySourceJob(choices[0].url,choices),{
    provider:'marriott',kind:'DISCOVER_CATALOG',priority:70,payload:{official_url:choices[0].url}});
  assert.deepEqual(helpers.directorySourceJob(choices[1].url,choices).payload,{official_url:choices[1].url});
  assert.throws(()=>helpers.directorySourceJob('https://example.com/',choices));
  assert.throws(()=>helpers.directorySourceJob(choices[0].url,undefined));
});

test('source action uses authenticated POST without changing the displayed active source',async()=>{
  const calls=[];
  const result=await helpers.requestDirectorySource(async(path,options)=>{
    calls.push({path,options});return {id:'offline-new-job',status:'PENDING'};
  },'offline-admin',choices[0].url,choices);
  assert.equal(result.status,'PENDING');
  assert.equal(calls[0].path,'/jobs');
  assert.equal(calls[0].options.method,'POST');
  assert.equal(calls[0].options.headers.authorization,'Bearer offline-admin');
  assert.deepEqual(JSON.parse(calls[0].options.body),helpers.directorySourceJob(choices[0].url,choices));
  assert.equal(catalog.selected_directory_root,choices[1].url);
});

test('pending and failed requests are not described as adopted sources',()=>{
  assert.equal(helpers.directoryRequestStatus(catalog),'尚无切换申请');
  const requested={...catalog,requested_directory_root:choices[0].url};
  assert.equal(helpers.directoryRequestStatus({...requested,requested_directory_root_status:'PENDING'}),'待调度');
  assert.equal(helpers.directoryRequestStatus({...requested,requested_directory_root_status:'RUNNING'}),'读取中');
  assert.equal(helpers.directoryRequestStatus({...requested,requested_directory_root_status:'FAILED'}),'读取失败，保留当前来源');
  assert.equal(helpers.directoryRequestStatus({...requested,requested_directory_root_status:'SUCCEEDED'}),'尚未采用');
  assert.equal(helpers.directoryRequestStatus({...requested,selected_directory_root:choices[0].url,requested_directory_root_status:'SUCCEEDED'}),'已验证采用');
});

const source=await readFile(new URL('./MarriottDirectorySource.vue',import.meta.url),'utf8');
const {descriptor}=parse(source);
const script=compileScript(descriptor,{id:'marriott-source-test',inlineTemplate:true}).content;
const body=script.replace(/import \{([^}]+)\} from ["']vue["'];?/g,(_,names)=>
  `const {${names.replace(/\s+as\s+/g,': ')}}=Vue;`
).replace(/import \{([^}]+)\} from ["']\.\/marriottDirectorySource.js["'];?/g,(_,names)=>
  `const {${names}}=helpers;`
).replace('export default','return');
const component=new Function('Vue','helpers',body)(Vue,helpers);
const request=async()=>{throw new Error('Rendering must not send requests');};

test('actual selector shows active English source separately from pending Chinese request',async()=>{
  const html=await renderToString(Vue.createSSRApp(component,{request,adminToken:'offline-admin',catalog:{...catalog,
    requested_directory_root:choices[0].url,requested_directory_root_status:'PENDING'}}));
  assert.ok(html.includes('当前采用：英文官网（全球目录）'));
  assert.ok(html.includes('最近申请：中文官网（全球目录） · 待调度'));
  assert.ok(html.includes('中文和英文官网均为全球目录入口'));
  assert.ok(html.includes('目录来源切换不代表全年报价已抓取完成'));
  assert.ok(!html.includes('offline-admin'));
  assert.match(html,/<button class="primary">核对并采用此来源<\/button>/);
});

test('selector requires management token and does not silently enable a disabled group',async()=>{
  const missingToken=await renderToString(Vue.createSSRApp(component,{request,catalog}));
  assert.match(missingToken,/<button class="primary" disabled>/);
  assert.ok(missingToken.includes('填写上方管理令牌'));
  const disabled=await renderToString(Vue.createSSRApp(component,{request,adminToken:'offline',catalog:{...catalog,enabled:false}}));
  assert.match(disabled,/<button class="primary" disabled>/);
  assert.ok(disabled.includes('不会修改启用设置或解除暂停'));
});

test('source failures stay visibly failed and rendered errors are escaped',async()=>{
  const html=await renderToString(Vue.createSSRApp(component,{request,catalog:{...catalog,
    requested_directory_root:choices[0].url,requested_directory_root_status:'FAILED',
    requested_directory_root_error:'<script>bad()</script>'}}));
  assert.ok(html.includes('读取失败，保留当前来源'));
  assert.ok(html.includes('&lt;script&gt;bad()&lt;/script&gt;'));
  assert.ok(!html.includes('<script>'));
});
