<script setup>
import {onMounted,onUnmounted,ref} from 'vue';
const status=ref(null),providers=ref([]),jobs=ref([]),error=ref(''),loading=ref(false);
let timer;
async function refresh(){
  if(loading.value)return;
  loading.value=true;
  try{const results=await Promise.all(['/dashboard','/providers','/jobs'].map(async p=>{const r=await fetch('/api'+p,{signal:AbortSignal.timeout(10000)});if(!r.ok)throw new Error('服务暂不可用：HTTP '+r.status);return r.json();}));[status.value,providers.value,jobs.value]=results;error.value='';}catch(e){error.value=e.message;}finally{loading.value=false;}
}
const names={marriott:'万豪',ihg:'IHG',hilton:'希尔顿',hyatt:'凯悦',accor:'雅高',gha:'GHA'};
const when=v=>v?new Date(v).toLocaleString('zh-CN'):'尚无记录';
onMounted(()=>{refresh();timer=setInterval(refresh,15000);});onUnmounted(()=>clearInterval(timer));
</script>
<template>
  <header><div><span class="eyebrow">SELF-HOSTED · PHASE 1</span><h1>Hotel Bug Price Monitor</h1><p>酒店价格情报平台 · 基础运行状态</p></div><button @click="refresh" :disabled="loading">{{loading?'刷新中…':'刷新状态'}}</button></header>
  <main>
    <p v-if="error" role="alert" class="notice error">{{error}}</p>
    <p class="notice">新版基础框架：PostgreSQL 历史存储、Redis 任务队列、独立调度和采集进程。真实酒店 Provider 将按阶段接入；未采集的数据不会用演示价格填充。</p>
    <section class="cards"><article><small>酒店库</small><strong>{{status?.hotels??'—'}}</strong></article><article><small>今日采集报价</small><strong>{{status?.rates_today??'—'}}</strong></article><article><small>待处理任务</small><strong>{{status?.queued_jobs??'—'}}</strong></article><article><small>已确认异常</small><strong>{{status?.confirmed_alerts??'—'}}</strong></article></section>
    <section class="panel"><h2>后台服务</h2><p>Worker 心跳：{{when(status?.worker_heartbeat)}}</p><p>Scheduler 心跳：{{when(status?.scheduler_heartbeat)}}</p><small>心跳表示进程在线，不代表官网报价已接入。</small></section>
    <section class="panel"><h2>Provider 状态</h2><div class="providers"><article v-for="p in providers" :key="p.provider"><h3>{{names[p.provider]}}</h3><span class="badge">{{p.implemented?p.status:'待研究 / 尚未实现'}}</span><p>{{p.last_error||'暂无错误'}}</p><small>今日任务 {{p.requests_today||0}} · 成功 {{p.success_count||0}}</small></article></div></section>
    <section class="panel"><h2>最近任务</h2><p v-if="!jobs.length" class="empty">暂无任务。尚未注册真实 Provider，不会自动发起官网请求。</p><div class="table" v-else><table><thead><tr><th>集团</th><th>类型</th><th>状态</th><th>计划时间</th></tr></thead><tbody><tr v-for="j in jobs" :key="j.id"><td>{{names[j.provider]}}</td><td>{{j.kind}}</td><td>{{j.status}}</td><td>{{when(j.scheduled_at)}}</td></tr></tbody></table></div></section>
    <footer>环境变量配置 · 旧 SQLite 数据未自动导入 · 价格日历和完整业务页面在后续阶段交付</footer>
  </main>
</template>
