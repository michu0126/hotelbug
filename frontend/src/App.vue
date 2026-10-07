<script setup>
import {computed,onMounted,onUnmounted,ref,watch} from 'vue';
import HistoryPanel from './HistoryPanel.vue';
import AnomalySettings from './AnomalySettings.vue';
import AlertPanel from './AlertPanel.vue';
import {discoveryPayload,urlProviders} from './hotelDiscovery.js';
import WatchlistPanel from './WatchlistPanel.vue';
import IhgSitemapProgress from './IhgSitemapProgress.vue';
import {catalogCoverage} from './catalogCoverage.js';
const watchRevision=ref(0);
const status=ref(null),providers=ref([]),catalogs=ref([]),jobs=ref([]),hotels=ref([]),watches=ref([]),alerts=ref([]),calendar=ref(null);
const error=ref(''),actionMessage=ref(''),loading=ref(false),actionBusy=ref(false);
const adminToken=ref(''),propertyCode=ref('0338'),selectedProvider=ref('accor'),hotelId=ref(''),daysAhead=ref(365);
const officialUrl=ref('');
const hotelSearch=ref('');
const historyDate=ref(''),historyOffer=ref('');
const needsOfficialUrl=computed(()=>urlProviders.includes(selectedProvider.value));
const telegramBotToken=ref(''),telegramChatId=ref(''),telegramConfigured=ref(false),telegramEnabled=ref(true),telegramDirty=ref(false),telegramMessage=ref('');
const monitoring=ref(null),monitoringMessage=ref(''),browserProxy=ref(''),telegramProxy=ref(''),clearBrowserProxy=ref(false),clearTelegramProxy=ref(false),dropPercent=ref(50);
const renotifyPercent=ref(20);
const today=new Date(),month=ref(`${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}`);
let timer;
async function api(path,options={}){
  const r=await fetch('/api'+path,{signal:AbortSignal.timeout(15000),...options});
  if(!r.ok){let detail='';try{const d=(await r.json()).detail;detail=Array.isArray(d)?d.map(e=>`${(e.loc||[]).filter(v=>v!=='body').join('.')}：${e.msg||'参数不正确'}`).join('；'):d||'';}catch{}throw new Error(`HTTP ${r.status}${detail?' · '+detail:''}`)}
  return r.json();
}
async function refresh(){
  if(loading.value)return;
  loading.value=true;
  try{
    const results=await Promise.all(['/dashboard','/providers','/jobs',`/hotels?limit=100&q=${encodeURIComponent(hotelSearch.value.trim())}`,'/watchlists','/alerts','/catalogs'].map(p=>api(p)));
    [status.value,providers.value,jobs.value,hotels.value,watches.value,alerts.value,catalogs.value]=results;
    watchRevision.value++;
    if(!hotelId.value&&hotels.value.length)hotelId.value=hotels.value[0].id;
    error.value='';
  }catch(e){error.value=e.message;}finally{loading.value=false;}
}
async function loadCalendar(){
  if(!hotelId.value){calendar.value=null;return;}
  const selectedHotel=hotelId.value,selectedMonth=month.value;
  try{
    const result=await api(`/hotels/${encodeURIComponent(selectedHotel)}/calendar?month=${selectedMonth}`);
    if(selectedHotel!==hotelId.value||selectedMonth!==month.value)return;
    calendar.value=result;error.value='';
    if(!historyDate.value)historyDate.value=result.days.find(d=>d.status==='AVAILABLE')?.date||selectedMonth+'-01';
  }
  catch(e){if(selectedHotel===hotelId.value&&selectedMonth===month.value){calendar.value=null;error.value=e.message;}}
}
function selectHistoryDay(day){historyDate.value=day.date;historyOffer.value=day.offer_key||'';}
async function searchHotels(){
  if(loading.value)return;
  hotelId.value='';
  await refresh();
}
async function discover(){
  actionBusy.value=true;actionMessage.value='';
  try{
    const payload=discoveryPayload(selectedProvider.value,needsOfficialUrl.value?officialUrl.value:propertyCode.value);
    const result=await api('/jobs',{method:'POST',headers:{'content-type':'application/json','authorization':`Bearer ${adminToken.value}`},
      body:JSON.stringify({provider:selectedProvider.value,kind:'DISCOVER_HOTELS',priority:60,payload})});
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
async function saveTelegram(){
  actionBusy.value=true;telegramMessage.value='';
  try{
    const result=await api('/settings/telegram',{method:'PUT',headers:{'content-type':'application/json','authorization':`Bearer ${adminToken.value}`},
      body:JSON.stringify({bot_token:telegramBotToken.value||null,chat_id:telegramChatId.value.trim(),enabled:telegramEnabled.value})});
    telegramConfigured.value=result.configured;
    telegramBotToken.value='';
    telegramDirty.value=false;
    telegramMessage.value=result.configured?'Telegram 设置已保存，确认降价后会使用此机器人发送。':'已保存 Chat ID；仍需填写机器人 Token。';
  }catch(e){telegramMessage.value=e.message;}finally{actionBusy.value=false;}
}
async function testTelegram(){
  actionBusy.value=true;telegramMessage.value='';
  try{
    await api('/settings/telegram/test',{method:'POST',headers:{authorization:`Bearer ${adminToken.value}`},signal:AbortSignal.timeout(25000)});
    telegramMessage.value='测试消息已由 Telegram 接收，请查看目标聊天。';
  }catch(e){telegramMessage.value=`测试发送失败：${e.message}`;}finally{actionBusy.value=false;}
}
async function loadTelegramSettings(){
  if(!adminToken.value)return;
  try{
    const saved=await api('/settings/telegram',{headers:{authorization:`Bearer ${adminToken.value}`}});
    telegramChatId.value=saved.chat_id||'';
    telegramConfigured.value=saved.configured;
    telegramEnabled.value=saved.enabled;
    telegramDirty.value=false;
  }catch(e){telegramMessage.value=e.message;}
}
async function loadMonitoringSettings(){
  if(!adminToken.value)return;
  const token=adminToken.value;
  try{
    const saved=await api('/settings/monitoring',{headers:{authorization:`Bearer ${token}`}});
    if(token!==adminToken.value)return;
    monitoring.value=saved;dropPercent.value=Number(saved.alert_drop_fraction)*100;
    renotifyPercent.value=Number(saved.alert_renotify_drop_fraction??0.2)*100;
    browserProxy.value='';telegramProxy.value='';clearBrowserProxy.value=false;clearTelegramProxy.value=false;
    monitoringMessage.value='已读取设置。修改并保存后，下一轮调度和新采价任务自动使用新设置。';
  }catch(e){monitoringMessage.value=e.message;}
}
async function loadManagementSettings(){
  await Promise.all([loadTelegramSettings(),loadMonitoringSettings()]);
}
async function saveMonitoringSettings(){
  if(!monitoring.value)return;
  actionBusy.value=true;monitoringMessage.value='';
  try{
    const body={
      providers:Object.fromEntries(Object.entries(monitoring.value.providers).map(([name,p])=>[name,{enabled:p.enabled,concurrency:Number(p.concurrency),interval_seconds:Number(p.interval_seconds)}])),
      global_monitoring_enabled:monitoring.value.global_monitoring_enabled,
      alert_drop_fraction:Number(dropPercent.value)/100,
      alert_renotify_drop_fraction:Number(renotifyPercent.value)/100,
      alert_historical_lows_enabled:monitoring.value.alert_historical_lows_enabled,
      anomaly_policy:monitoring.value.anomaly_policy,
      alert_min_history_days:Number(monitoring.value.alert_min_history_days),
      alert_confirmation_seconds:Number(monitoring.value.alert_confirmation_seconds),
      browser_proxy_url:clearBrowserProxy.value?'':browserProxy.value.trim()||null,
      telegram_proxy_url:clearTelegramProxy.value?'':telegramProxy.value.trim()||null,
    };
    monitoring.value=await api('/settings/monitoring',{method:'PUT',headers:{'content-type':'application/json','authorization':`Bearer ${adminToken.value}`},body:JSON.stringify(body)});
    browserProxy.value='';telegramProxy.value='';clearBrowserProxy.value=false;clearTelegramProxy.value=false;
    monitoringMessage.value='已保存，无需重启容器。正在执行的任务完成后，新任务使用新设置；关闭集团会取消其尚未开始的任务。';
    await refresh();
  }catch(e){monitoringMessage.value=e.message;}finally{actionBusy.value=false;}
}
const names={marriott:'万豪',ihg:'IHG',hilton:'希尔顿',hyatt:'凯悦',accor:'雅高',gha:'GHA'};
const when=v=>v?new Date(v).toLocaleString('zh-CN'):'尚无记录';
const calendarDays=computed(()=>calendar.value?.days||[]);
watch([hotelId,month],()=>{calendar.value=null;historyDate.value='';historyOffer.value='';loadCalendar();});
watch(adminToken,()=>{monitoring.value=null;monitoringMessage.value='';browserProxy.value='';telegramProxy.value='';});
onMounted(()=>{refresh().then(loadCalendar);timer=setInterval(()=>{refresh().then(loadCalendar)},30000);});
onUnmounted(()=>clearInterval(timer));
</script>
<template>
  <header><div><span class="eyebrow">SELF-HOSTED · HOTEL PRICE MONITOR</span><h1>Hotel Bug Price Monitor</h1><p>酒店官网价格日历与降价提醒</p></div><button @click="refresh" :disabled="loading">{{loading?'刷新中…':'刷新状态'}}</button></header>
  <main>
    <p v-if="error" role="alert" class="notice error">{{error}}</p>
    <p class="notice">官网采集正在逐集团验证。{{status?.global_monitoring_enabled===false?'全球轮询已关闭，已设置的酒店监控仍可运行。':'已收录酒店按未来一年轮询。'}}同条件价格较历史基准下降 {{Math.round(Number(status?.alert_drop_fraction??0.5)*100)}}% 时自动复查，确认后通过 Telegram 通知。历史基准至少需要 {{status?.alert_min_history_days??3}} 天记录。</p>
    <section class="cards"><article><small>酒店库</small><strong>{{status?.hotels??'—'}}</strong></article><article><small>今日采集报价</small><strong>{{status?.rates_today??'—'}}</strong></article><article><small>待处理任务</small><strong>{{status?.queued_jobs??'—'}}</strong></article><article><small>已确认异常</small><strong>{{status?.confirmed_alerts??'—'}}</strong></article></section>
    <section class="panel"><h2>酒店与价格日历</h2><p class="muted">启用对应集团并设置管理令牌后可提交任务。雅高与 GHA 已通过官网单酒店采集入库实测；其他集团的状态见下方。</p>
      <div class="fields"><label>管理令牌<input v-model="adminToken" type="password" autocomplete="off" placeholder="仅保存在此页面内存" @blur="loadManagementSettings"></label><label>酒店集团<select v-model="selectedProvider"><option value="accor">雅高</option><option value="gha">GHA</option><option value="marriott">万豪（实验）</option><option value="ihg">IHG（实验）</option><option value="hilton">希尔顿（实验）</option><option value="hyatt">凯悦（独立采集待验证）</option></select></label><label v-if="needsOfficialUrl">{{selectedProvider==='hyatt'?'凯悦官网酒店详情链接':'官网英文酒店详情链接'}}<input v-model="officialUrl" type="url" :placeholder="selectedProvider==='hyatt'?'https://www.hyatt.com/…/zh-CN/酒店代码-名称':'https://www.ihg.com/…/hoteldetail'"></label><label v-else>酒店代码<input v-model="propertyCode" maxlength="10" placeholder="雅高 0338 / GHA 10624"></label><button class="primary" :disabled="actionBusy" @click="discover">添加酒店资料</button></div>
      <p v-if="selectedProvider==='hyatt'" class="muted">凯悦全球列表目录与中文单晚报价解析已接入，独立 Worker 仍待验证；正常浏览器目录可读不等于自动采价成功。详情入口支持官网新旧格式及合作品牌，特殊预订页尚未验收；运行结果以实际酒店和报价入库为准。</p>
      <div class="fields"><label>搜索已收录的全球酒店<input v-model="hotelSearch" placeholder="酒店名称、国家、城市或品牌" @keyup.enter="searchHotels"></label><button :disabled="loading" @click="searchHotels">搜索酒店</button><small>最多显示 100 个匹配结果，输入关键词查找其他酒店</small></div>
      <div class="fields"><label>酒店<select v-model="hotelId"><option value="">请选择</option><option v-for="h in hotels" :key="h.id" :value="h.id">{{h.hotel_name}} · {{h.city||h.provider_hotel_id}}</option></select></label><label>月份<input v-model="month" type="month"></label><button class="primary" :disabled="actionBusy||!hotelId" @click="crawlMonth">采集本月报价</button></div>
      <div class="fields"><label>自动监控未来天数<select v-model="daysAhead"><option :value="30">30 天</option><option :value="90">90 天</option><option :value="365">365 天</option></select></label><button class="primary" :disabled="actionBusy||!hotelId" @click="watchHotel">保存自动监控</button><small>完整监控范围及状态见下方列表</small></div>
      <p v-if="actionMessage" role="status" class="notice">{{actionMessage}}</p>
      <div v-if="calendar" class="calendar">
        <article v-for="d in calendarDays" :key="d.date" class="day">
          <small>{{Number(d.date.slice(-2))}} 日</small>
          <strong v-if="d.status==='AVAILABLE'">{{d.currency}} {{d.current_low}}</strong>
          <span v-else>{{d.status==='NOT_OPEN'?'已查：尚未开放预订':d.status==='UNAVAILABLE'?'已查：无可售房':'尚未取得报价'}}</span>
          <small v-if="d.status==='AVAILABLE'">{{d.price_field==='cash_price'?'官网展示价 · 全部税费未确认':'含税费'}}</small>
          <small v-if="d.historical_low">同报价历史低 {{d.historical_low}}</small>
          <small v-if="d.drop_from_previous_percent">较上次 {{d.drop_from_previous_percent}}%</small>
          <button v-if="d.status==='AVAILABLE'" class="history-day-button" @click="selectHistoryDay(d)">查看历史</button>
        </article>
      </div>
      <p v-else-if="!hotels.length" class="empty">尚未收录酒店。</p>
    </section>
    <HistoryPanel :hotel-id="hotelId" v-model:check-in="historyDate" :preferred-offer="historyOffer"/>
    <WatchlistPanel :admin-token="adminToken" :group-names="names" :request="api" :revision="watchRevision" @changed="refresh"/>
    <AlertPanel :alerts="alerts" :group-names="names"/>
    <section class="panel"><h2>Telegram 通知设置</h2><p class="muted">填写上面的管理令牌后保存；机器人 Token 留空表示保留已保存的值。保存后可手动发送一条测试消息。</p><div class="fields"><label>机器人 Token<input v-model="telegramBotToken" type="password" autocomplete="off" placeholder="BotFather 提供的 Token" @input="telegramDirty=true"></label><label>Chat ID<input v-model="telegramChatId" autocomplete="off" placeholder="接收消息的 Chat ID" @input="telegramDirty=true"></label><label>启用通知<input v-model="telegramEnabled" type="checkbox"></label><button class="primary" :disabled="actionBusy" @click="saveTelegram">保存 Telegram 设置</button><button :disabled="actionBusy||!telegramConfigured||telegramDirty" @click="testTelegram">发送测试消息</button><small>{{telegramConfigured?'已配置':'尚未在此页面保存'}}</small></div><p v-if="telegramMessage" role="status" class="notice">{{telegramMessage}}</p></section>
    <section class="panel"><h2>后台服务</h2><p>Worker 心跳：{{when(status?.worker_heartbeat)}}</p><p>Scheduler 心跳：{{when(status?.scheduler_heartbeat)}}</p><small>心跳只表示进程在线。请以 Provider 状态和实际报价为准。</small></section>
    <section class="panel"><h2>自动采集设置</h2><p class="muted">填写管理令牌后读取设置。监测范围固定为未来 365 天，时区内置为 Asia/Shanghai。页面设置优先于环境变量，保存后无需重建容器；关闭集团不会删除酒店或历史价格。</p><button :disabled="actionBusy||!adminToken" @click="loadMonitoringSettings">读取已保存设置</button>
      <template v-if="monitoring">
        <div class="fields"><label>历史新低独立提醒<input v-model="monitoring.alert_historical_lows_enabled" type="checkbox"></label><small>默认开启；至少积累指定历史天数，独立复查后才通知。与降价提醒共用再次下降比例，避免重复推送。</small></div>
        <AnomalySettings v-if="monitoring.anomaly_policy" v-model:policy="monitoring.anomaly_policy"/>
        <div class="fields"><label>全球酒店自动轮询<input v-model="monitoring.global_monitoring_enabled" type="checkbox"></label><label>降价提醒阈值（%）<input v-model="dropPercent" type="number" min="1" max="99" step="1"></label><label>再次提醒需额外下降（%）<input v-model="renotifyPercent" type="number" min="1" max="99" step="1"></label><label>至少历史天数<input v-model="monitoring.alert_min_history_days" type="number" min="2" max="90"></label><label>独立复查间隔（秒）<input v-model="monitoring.alert_confirmation_seconds" type="number" min="5" max="3600"></label></div>
        <div class="providers"><article v-for="(p,name) in monitoring.providers" :key="name"><h3>{{names[name]}}</h3><label>启用官网采集<input v-model="p.enabled" type="checkbox" :disabled="!p.implemented"></label><p v-if="!p.implemented" class="muted">采集器尚未完成，暂不能启用。</p><label>最短请求间隔（秒）<input v-model="p.interval_seconds" type="number" min="1" max="3600" :disabled="!p.implemented"></label><label>集团并发数<input v-model="p.concurrency" type="number" min="1" max="4" :disabled="!p.implemented"></label></article></div>
        <div class="fields"><label>采集 HTTP 代理<input v-model="browserProxy" type="password" autocomplete="off" :disabled="clearBrowserProxy" placeholder="http://192.168.50.4:7890（留空保留）"><small>当前：{{monitoring.browser_proxy_display||'未设置，使用容器网络'}}</small></label><label>清除采集代理<input v-model="clearBrowserProxy" type="checkbox"></label><label>Telegram 专用 HTTP 代理<input v-model="telegramProxy" type="password" autocomplete="off" :disabled="clearTelegramProxy" placeholder="留空保留；未设置时沿用采集代理"><small>当前：{{monitoring.telegram_proxy_display||'沿用采集代理或容器网络'}}</small></label><label>清除 Telegram 专用代理<input v-model="clearTelegramProxy" type="checkbox"></label><button class="primary" :disabled="actionBusy" @click="saveMonitoringSettings">保存自动采集设置</button></div>
      </template><p v-if="monitoringMessage" role="status" class="notice">{{monitoringMessage}}</p>
    </section>
    <section class="panel"><h2>Provider 状态</h2><div class="providers"><article v-for="p in providers" :key="p.provider"><h3>{{names[p.provider]}}</h3><span class="badge">{{p.implemented?(p.enabled?p.status:'已关闭 · '+p.status):'待研究 / 尚未实现'}}</span><p>{{p.last_error||p.verification}}</p><small>今日任务 {{p.requests_today||0}} · 成功 {{p.success_count||0}}</small></article></div></section>
    <section class="panel"><h2>全球官网目录接入进度</h2><p class="muted">已启用集团自动沿官网目录或地图发现酒店。目录按当前公开链接逐页核对并按酒店代码去重；目录全量核对不代表一年房价已全量抓取。取得报价的酒店单独统计。</p><div class="table"><table><thead><tr><th>集团</th><th>已收录酒店</th><th>取得报价的酒店</th><th>有效核对 / 发现目录</th><th>候选资料本轮已派发 / 总数</th><th>目录核对状态</th><th>目录状态 / 最近问题</th></tr></thead><tbody><tr v-for="c in catalogs" :key="c.provider"><td>{{names[c.provider]}}</td><td>{{c.hotels}}<div v-if="c.official_worldwide_hotels!=null" class="muted">官网总数 {{c.official_worldwide_hotels}}<br>目录去重 {{c.directory_unique_hotels}}</div></td><td>{{c.hotels_with_quotes}}</td><td>{{c.source==='OFFICIAL_BROWSER_DIRECTORY'?`${c.complete_directory_pages} / ${c.reachable_directory_pages}`:c.source==='OFFICIAL_SITEMAP'?`${c.sitemaps_read} / ${c.sitemaps_total}`:'—'}}</td><td>{{c.source==='OFFICIAL_SITEMAP'?`${c.candidate_tasks_dispatched} / ${c.hotel_candidates}`:'—'}}</td><td>{{catalogCoverage(c)}}</td><td>{{c.last_error||(c.source==='NOT_IMPLEMENTED'?'尚未实现':c.enabled?'自动发现中':'默认关闭')}}</td></tr></tbody></table></div></section>
    <section class="panel"><h2>最近任务</h2><p v-if="!jobs.length" class="empty">暂无任务。</p><div class="table" v-else><table><thead><tr><th>集团</th><th>类型</th><th>状态</th><th>计划时间</th><th>错误</th></tr></thead><tbody><tr v-for="j in jobs" :key="j.id"><td>{{names[j.provider]}}</td><td>{{j.kind}}</td><td>{{j.status}}</td><td>{{when(j.scheduled_at)}}</td><td>{{j.error_type||'—'}}</td></tr></tbody></table></div></section>
    <IhgSitemapProgress :progress="catalogs.find(c=>c.provider==='ihg')?.auxiliary_sitemap" />
    <p v-for="c in catalogs.filter(c=>c.unparsed_hotel_links>0)" :key="'unparsed-'+c.provider" class="notice">{{names[c.provider]}}目录有 {{c.unparsed_hotel_links}} 个链接未能解析酒店身份，未计为成功收录；目录仍未完整核对。</p>
    <footer>价格来自实际成功采集；无数据即显示空白 · Telegram 机器人可在本页面设置</footer>
  </main>
</template>
