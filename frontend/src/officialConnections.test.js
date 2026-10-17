import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import * as Vue from 'vue';
import {parse,compileScript} from '@vue/compiler-sfc';
import {renderToString} from 'vue/server-renderer';

const source=await readFile(new URL('./OfficialConnections.vue',import.meta.url),'utf8');
const {descriptor}=parse(source);
const script=compileScript(descriptor,{id:'official-connection-test',inlineTemplate:true}).content;
const body=script.replace(/import \{([^}]+)\} from ["']vue["'];?/g,(_,names)=>`const {${names.replace(/\s+as\s+/g,': ')}}=Vue;`).replace('export default','return');
const component=new Function('Vue',body)(Vue);
const props={adminToken:'PRIVATE-ADMIN',groupNames:{marriott:'万豪',ihg:'IHG'},hotels:[],request:async()=>{throw new Error('Rendering must not fetch credentials');}};

test('renders local Cookie connector without exposing token or claiming connected',async()=>{
  const html=await renderToString(Vue.createSSRApp(component,props));
  assert.ok(html.includes('官网会话连接')&&html.includes('JSON 或 Netscape'));
  assert.ok(!html.includes('PRIVATE-ADMIN'));
  assert.ok(html.includes('尚未读取连接状态'));
  assert.ok(html.includes('保存 Cookie 不会自动解决所有官网限制'));
  assert.match(html,/<textarea[^>]*autocomplete="off"/);
  assert.match(html,/<button[^>]*disabled[^>]*>测试实际报价<\/button>/);
});

test('Cookie values are never stored in localStorage or generic settings',()=>{
  assert.ok(!source.includes('localStorage')&&!source.includes('sessionStorage'));
  assert.ok(source.includes("cookieText.value=''"));
  assert.ok(source.includes('/settings/connections/'));
  assert.ok(!source.includes('/settings/monitoring'));
});
