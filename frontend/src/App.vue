<script setup>
import {computed,onMounted,onUnmounted,ref,watch} from 'vue';
const status=ref(null),providers=ref([]),jobs=ref([]),hotels=ref([]),watches=ref([]),calendar=ref(null);
const error=ref(''),actionMessage=ref(''),loading=ref(false),actionBusy=ref(false);
const adminToken=ref(''),propertyCode=ref('NYCMQ'),hotelId=ref(''),daysAhead=ref(30);
const today=new Date(),month=ref(`${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}`);
let timer;
async function api(path,options={}){
  const r=await fetch('/api'+path,{signal:AbortSignal.timeout(15000),...options});
  if(!r.ok){let detail='';try{detail=(await r.json()).detail||'';}catch{}throw new Error(`HTTP ${r.status}${detail?' · '+detail:''}`)}
  return r.json();
}
async function refresh(){
  if(loading.value)return;
  loading.value=true;
  try{
    const results=await Promise.all(['/dashboard','/providers','/jobs','/hotels?provider=marriott&limit=100','/watchlists'].map(p=>api(p)));
    [status.value,providers.value,jobs.value,hotels.value,watches.value]=results;
    if(!hotelId.value&&hotels.value.length)hotelId.value=hotels.value[0].id;
    error.value='';
  }catch(e){error.value=e.message;}finally{loading.value=false;}
}
async function loadCalendar(){
  if(!hotelId.value){calendar.value=null;return;}
  try{calendar.value=await api(`/hotels/${encodeURIComponent(hotelId.value)}/calendar?month=${month.value}`);error.value='';}
  catch(e){calendar.value=null;error.value=e.message;}
}
async function discover(){
  actionBusy.value=true;actionMessage.value='';
  try{
    const code=propertyCode.value.trim().toUpperCase();
    if(!/^[A-Z0-9]{5}$/.test(code))throw new Error('请输入 5 位万豪酒店代码');
    const result=await api('/jobs',{method:'POST',headers:{'content-type':'application/json','authorization':`Bearer ${adminToken.value}`},
      body:JSON.stringify({provider:'marriott',kind:'DISCOVER_HOTELS',priority:60,payload:{provider_hotel_id:code}})});
    actionMessage.value=`酒店资料任务已提交：${result.id}。完成后刷新状态。`;
    await refresh();
  }catch(e){actionMessage.value=e.message;}finally{actionBusy.value=false;}
}
async function crawlMonth(){
  if(!hotelId.value)return;
  actionBusy.value=true;actionMessage.value='';
  try{
    const result=await api(`/hotels/${encodeURIComponent(hotelId.value)}/calendar/jobs?month=${month.value}`,
      {method:'POST',headers:{'authorization':`Bearer ${adminToken.value}`}});
    actionMessage.value=`已提交 ${result.queued_dates} 个逐日任务。官网限流会使其分批完成。`;
    await refresh();
  }catch(e){actionMessage.value=e.message;}finally{actionBusy.value=false;}
}
async function watchHotel(){
  if(!hotelId.value)return;
  actionBusy.value=true;actionMessage.value='';
  try{
    await api('/watchlists',{method:'POST',headers:{'content-type':'application/json','authorization':`Bearer ${adminToken.value}`},
      body:JSON.stringify({hotel_id:hotelId.value,days_ahead:Number(daysAhead.value)})});
    actionMessage.value='自动监控已保存。Scheduler 将按 HOT/WARM/COLD 周期逐日生成任务。';
    await refresh();
  }catch(e){actionMessage.value=e.message;}finally{actionBusy.value=false;}
}
const names={marriott:'万豪',ihg:'IHG',hilton:'希尔顿',hyatt:'凯悦',accor:'雅高',gha:'GHA'};
const when=v=>v?new Date(v).toLocaleString('zh-CN'):'尚无记录';
const calendarDays=computed(()=>calendar.value?.days||[]);
watch([hotelId,month],loadCalendar);
onMounted(()=>{refresh().then(loadCalendar);timer=setInterval(()=>{refresh().then(loadCalendar)},30000);});
onUnmounted(()=>clearInterval(timer));
</script>
<template>
  <header><div><span class="eyebrow">SELF-HOSTED · PHASE 2 PREVIEW</span><h1>Hotel Bug Price Monitor</h1><p>酒店价格情报平台 · 万豪价格验证版</p></div><button @click="refresh" :disabled="loading">{{loading?'刷新中…':'刷新状态'}}</button></header>
  <main>
    <p v-if="error" role="alert" class="notice error">{{error}}</p>
    <p class="notice">万豪公开报价结构已按官网实测接入，但独立 HTTP 报价访问尚未验证成功，默认关闭。只有实际成功采集的价格才会出现在日历；异常通知将在后续阶段开放。</p>
    <section class="cards"><article><small>酒店库</small><strong>{{status?.hotels??'—'}}</strong></article><article><small>今日采集报价</small><strong>{{status?.rates_today??'—'}}</strong></article><article><small>待处理任务</small><strong>{{status?.queued_jobs??'—'}}</strong></article><article><small>已确认异常</small><strong>{{status?.confirmed_alerts??'—'}}</strong></article></section>
    <section class="panel"><h2>万豪酒店与价格日历</h2><p class="muted">启用 MARRIOTT_ENABLED=true 并在服务器设置 ADMIN_TOKEN 后，才可提交采集任务。403 会暂停该 Provider。</p>
      <div class="fields"><label>管理令牌<input v-model="adminToken" type="password" autocomplete="off" placeholder="仅保存在此页面内存"></label><label>万豪酒店代码<input v-model="propertyCode" maxlength="5" placeholder="例如 NYCMQ"></label><button class="primary" :disabled="actionBusy" @click="discover">添加酒店资料</button></div>
      <div class="fields"><label>酒店<select v-model="hotelId"><option value="">请选择</option><option v-for="h in hotels" :key="h.id" :value="h.id">{{h.hotel_name}} · {{h.city||h.provider_hotel_id}}</option></select></label><label>月份<input v-model="month" type="month"></label><button class="primary" :disabled="actionBusy||!hotelId" @click="crawlMonth">采集本月报价</button></div>
      <div class="fields"><label>自动监控未来天数<select v-model="daysAhead"><option :value="30">30 天</option><option :value="90">90 天</option><option :value="365">365 天</option></select></label><button class="primary" :disabled="actionBusy||!hotelId" @click="watchHotel">保存自动监控</button><small>已启用酒店监控 {{watches.filter(w=>w.enabled).length}} 项</small></div>
      <p v-if="actionMessage" role="status" class="notice">{{actionMessage}}</p>
      <div v-if="calendar" class="calendar"><article v-for="d in calendarDays" :key="d.date" class="day"><small>{{Number(d.date.slice(-2))}} 日</small><strong v-if="d.status==='AVAILABLE'">{{d.currency}} {{d.current_low}}</strong><span v-else>尚无数据</span><small v-if="d.historical_low">同报价历史低 {{d.historical_low}}</small><small v-if="d.drop_from_previous_percent">较上次 {{d.drop_from_previous_percent}}%</small></article></div>
      <p v-else-if="!hotels.length" class="empty">尚未收录万豪酒店。</p>
    </section>
    <section class="panel"><h2>后台服务</h2><p>Worker 心跳：{{when(status?.worker_heartbeat)}}</p><p>Scheduler 心跳：{{when(status?.scheduler_heartbeat)}}</p><small>心跳只表示进程在线。请以 Provider 状态和实际报价为准。</small></section>
    <section class="panel"><h2>Provider 状态</h2><div class="providers"><article v-for="p in providers" :key="p.provider"><h3>{{names[p.provider]}}</h3><span class="badge">{{p.implemented?(p.enabled?p.status:'默认关闭 · '+p.status):'待研究 / 尚未实现'}}</span><p>{{p.last_error||p.verification}}</p><small>今日任务 {{p.requests_today||0}} · 成功 {{p.success_count||0}}</small></article></div></section>
    <section class="panel"><h2>最近任务</h2><p v-if="!jobs.length" class="empty">暂无任务。</p><div class="table" v-else><table><thead><tr><th>集团</th><th>类型</th><th>状态</th><th>计划时间</th><th>错误</th></tr></thead><tbody><tr v-for="j in jobs" :key="j.id"><td>{{names[j.provider]}}</td><td>{{j.kind}}</td><td>{{j.status}}</td><td>{{when(j.scheduled_at)}}</td><td>{{j.error_type||'—'}}</td></tr></tbody></table></div></section>
    <footer>价格来自实际成功采集；无数据即显示空白 · 旧 SQLite 数据未自动导入 · 异常推送尚未启用</footer>
  </main>
</template>
