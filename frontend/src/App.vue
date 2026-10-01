<script setup>
import {computed,onMounted,onUnmounted,ref,watch} from 'vue';
const status=ref(null),providers=ref([]),catalogs=ref([]),jobs=ref([]),hotels=ref([]),watches=ref([]),alerts=ref([]),calendar=ref(null);
const error=ref(''),actionMessage=ref(''),loading=ref(false),actionBusy=ref(false);
const adminToken=ref(''),propertyCode=ref('0338'),selectedProvider=ref('accor'),hotelId=ref(''),daysAhead=ref(365);
const officialUrl=ref('');
const hotelSearch=ref('');
const needsOfficialUrl=computed(()=>['ihg','hilton'].includes(selectedProvider.value));
const telegramBotToken=ref(''),telegramChatId=ref(''),telegramConfigured=ref(false),telegramEnabled=ref(true),telegramDirty=ref(false),telegramMessage=ref('');
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
    const results=await Promise.all(['/dashboard','/providers','/jobs',`/hotels?limit=100&q=${encodeURIComponent(hotelSearch.value.trim())}`,'/watchlists','/alerts','/catalogs'].map(p=>api(p)));
    [status.value,providers.value,jobs.value,hotels.value,watches.value,alerts.value,catalogs.value]=results;
    if(!hotelId.value&&hotels.value.length)hotelId.value=hotels.value[0].id;
    error.value='';
  }catch(e){error.value=e.message;}finally{loading.value=false;}
}
async function loadCalendar(){
  if(!hotelId.value){calendar.value=null;return;}
  try{calendar.value=await api(`/hotels/${encodeURIComponent(hotelId.value)}/calendar?month=${month.value}`);error.value='';}
  catch(e){calendar.value=null;error.value=e.message;}
}
async function searchHotels(){
  if(loading.value)return;
  hotelId.value='';
  await refresh();
}
async function discover(){
  actionBusy.value=true;actionMessage.value='';
  try{
    let payload;
    if(needsOfficialUrl.value){
      let url;try{url=new URL(officialUrl.value.trim())}catch{throw new Error('请粘贴酒店官网的酒店详情链接')}
      const valid=selectedProvider.value==='ihg'
        ?url.hostname==='www.ihg.com'&&/^\/[a-z0-9-]+\/hotels\/[a-z]{2}\/en\/[a-z0-9-]+\/[a-z0-9]{5}\/hoteldetail\/?$/.test(url.pathname)
        :url.hostname==='www.hilton.com'&&/^\/en\/hotels\/[a-z0-9-]+\/$/.test(url.pathname);
      if(url.protocol!=='https:'||!valid||url.search||url.hash)throw new Error('请使用官网英文酒店详情页，不要粘贴带日期的搜索或预订链接');
      payload={official_url:url.href};
    }else{
      const code=propertyCode.value.trim().toUpperCase();
      const pattern={accor:/^[A-Z0-9]{4}$/,marriott:/^[A-Z0-9]{5}$/,gha:/^[1-9][0-9]{0,9}$/}[selectedProvider.value];
      if(!pattern?.test(code))throw new Error('雅高请输入 4 位代码，万豪 5 位代码，GHA 输入预订链接中的数字 hotelId');
      payload={provider_hotel_id:code};
    }
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
const names={marriott:'万豪',ihg:'IHG',hilton:'希尔顿',hyatt:'凯悦',accor:'雅高',gha:'GHA'};
const when=v=>v?new Date(v).toLocaleString('zh-CN'):'尚无记录';
const calendarDays=computed(()=>calendar.value?.days||[]);
watch([hotelId,month],loadCalendar);
onMounted(()=>{refresh().then(loadCalendar);timer=setInterval(()=>{refresh().then(loadCalendar)},30000);});
onUnmounted(()=>clearInterval(timer));
</script>
<template>
  <header><div><span class="eyebrow">SELF-HOSTED · HOTEL PRICE MONITOR</span><h1>Hotel Bug Price Monitor</h1><p>酒店官网价格日历与降价提醒</p></div><button @click="refresh" :disabled="loading">{{loading?'刷新中…':'刷新状态'}}</button></header>
  <main>
    <p v-if="error" role="alert" class="notice error">{{error}}</p>
    <p class="notice">官网采集正在逐集团验证。已收录酒店按未来一年轮询；同条件价格较历史基准下降 50% 时自动复查，确认后通过 Telegram 通知。历史基准至少需要 3 天记录。</p>
    <section class="cards"><article><small>酒店库</small><strong>{{status?.hotels??'—'}}</strong></article><article><small>今日采集报价</small><strong>{{status?.rates_today??'—'}}</strong></article><article><small>待处理任务</small><strong>{{status?.queued_jobs??'—'}}</strong></article><article><small>已确认异常</small><strong>{{status?.confirmed_alerts??'—'}}</strong></article></section>
    <section class="panel"><h2>酒店与价格日历</h2><p class="muted">启用对应集团并设置管理令牌后可提交任务。雅高与 GHA 已通过官网单酒店采集入库实测；其他集团的状态见下方。</p>
      <div class="fields"><label>管理令牌<input v-model="adminToken" type="password" autocomplete="off" placeholder="仅保存在此页面内存" @blur="loadTelegramSettings"></label><label>酒店集团<select v-model="selectedProvider"><option value="accor">雅高</option><option value="gha">GHA</option><option value="marriott">万豪（实验）</option><option value="ihg">IHG（实验）</option><option value="hilton">希尔顿（实验）</option></select></label><label v-if="needsOfficialUrl">官网英文酒店详情链接<input v-model="officialUrl" type="url" placeholder="https://www.ihg.com/…/hoteldetail"></label><label v-else>酒店代码<input v-model="propertyCode" maxlength="10" placeholder="雅高 0338 / GHA 10624"></label><button class="primary" :disabled="actionBusy" @click="discover">添加酒店资料</button></div>
      <div class="fields"><label>搜索已收录的全球酒店<input v-model="hotelSearch" placeholder="酒店名称、国家、城市或品牌" @keyup.enter="searchHotels"></label><button :disabled="loading" @click="searchHotels">搜索酒店</button><small>最多显示 100 个匹配结果，输入关键词查找其他酒店</small></div>
      <div class="fields"><label>酒店<select v-model="hotelId"><option value="">请选择</option><option v-for="h in hotels" :key="h.id" :value="h.id">{{h.hotel_name}} · {{h.city||h.provider_hotel_id}}</option></select></label><label>月份<input v-model="month" type="month"></label><button class="primary" :disabled="actionBusy||!hotelId" @click="crawlMonth">采集本月报价</button></div>
      <div class="fields"><label>自动监控未来天数<select v-model="daysAhead"><option :value="30">30 天</option><option :value="90">90 天</option><option :value="365">365 天</option></select></label><button class="primary" :disabled="actionBusy||!hotelId" @click="watchHotel">保存自动监控</button><small>已启用酒店监控 {{watches.filter(w=>w.enabled).length}} 项</small></div>
      <p v-if="actionMessage" role="status" class="notice">{{actionMessage}}</p>
      <div v-if="calendar" class="calendar">
        <article v-for="d in calendarDays" :key="d.date" class="day">
          <small>{{Number(d.date.slice(-2))}} 日</small>
          <strong v-if="d.status==='AVAILABLE'">{{d.currency}} {{d.current_low}}</strong>
          <span v-else>{{d.status==='NOT_OPEN'?'已查：尚未开放预订':d.status==='UNAVAILABLE'?'已查：无可售房':'尚未取得报价'}}</span>
          <small v-if="d.status==='AVAILABLE'">{{d.price_field==='cash_price'?'官网展示价 · 全部税费未确认':'含税费'}}</small>
          <small v-if="d.historical_low">同报价历史低 {{d.historical_low}}</small>
          <small v-if="d.drop_from_previous_percent">较上次 {{d.drop_from_previous_percent}}%</small>
        </article>
      </div>
      <p v-else-if="!hotels.length" class="empty">尚未收录酒店。</p>
    </section>
    <section class="panel"><h2>降价提醒</h2><p v-if="!alerts.length" class="empty">暂无降价事件。积累历史报价后自动比较并复查。</p><div class="table" v-else><table><thead><tr><th>酒店 / 集团</th><th>入住日期</th><th>当前价格</th><th>历史基准</th><th>复查结果</th></tr></thead><tbody><tr v-for="a in alerts" :key="a.id"><td>{{a.payload.hotel_name}} · {{names[a.payload.provider]}}</td><td>{{a.payload.check_in}}</td><td>{{a.payload.price}} {{a.payload.currency}}<small v-if="a.payload.price_field==='cash_price'"> · 官网展示价，全部税费未确认</small></td><td>{{a.payload.baseline}} {{a.payload.currency}}</td><td>{{a.confirmed?'已确认':a.payload.verification==='REJECTED'?'价格已变化':'待复查'}}</td></tr></tbody></table></div></section>
    <section class="panel"><h2>Telegram 通知设置</h2><p class="muted">填写上面的管理令牌后保存；机器人 Token 留空表示保留已保存的值。保存后可手动发送一条测试消息。</p><div class="fields"><label>机器人 Token<input v-model="telegramBotToken" type="password" autocomplete="off" placeholder="BotFather 提供的 Token" @input="telegramDirty=true"></label><label>Chat ID<input v-model="telegramChatId" autocomplete="off" placeholder="接收消息的 Chat ID" @input="telegramDirty=true"></label><label>启用通知<input v-model="telegramEnabled" type="checkbox"></label><button class="primary" :disabled="actionBusy" @click="saveTelegram">保存 Telegram 设置</button><button :disabled="actionBusy||!telegramConfigured||telegramDirty" @click="testTelegram">发送测试消息</button><small>{{telegramConfigured?'已配置':'尚未在此页面保存'}}</small></div><p v-if="telegramMessage" role="status" class="notice">{{telegramMessage}}</p></section>
    <section class="panel"><h2>后台服务</h2><p>Worker 心跳：{{when(status?.worker_heartbeat)}}</p><p>Scheduler 心跳：{{when(status?.scheduler_heartbeat)}}</p><small>心跳只表示进程在线。请以 Provider 状态和实际报价为准。</small></section>
    <section class="panel"><h2>Provider 状态</h2><div class="providers"><article v-for="p in providers" :key="p.provider"><h3>{{names[p.provider]}}</h3><span class="badge">{{p.implemented?(p.enabled?p.status:'默认关闭 · '+p.status):'待研究 / 尚未实现'}}</span><p>{{p.last_error||p.verification}}</p><small>今日任务 {{p.requests_today||0}} · 成功 {{p.success_count||0}}</small></article></div></section>
    <section class="panel"><h2>全球官网目录接入进度</h2><p class="muted">IHG 启用后自动沿官网全球地区及城市目录发现酒店。目录收录数量不等于已取得价格；报价酒店数量单独统计。</p><div class="table"><table><thead><tr><th>集团</th><th>已收录酒店</th><th>取得报价的酒店</th><th>已解析 / 已发现目录</th><th>目录完整性未确认</th><th>目录状态 / 最近问题</th></tr></thead><tbody><tr v-for="c in catalogs" :key="c.provider"><td>{{names[c.provider]}}</td><td>{{c.hotels}}</td><td>{{c.hotels_with_quotes}}</td><td>{{c.source==='OFFICIAL_BROWSER_DIRECTORY'?`${c.parsed_directory_pages} / ${c.directory_pages}`:'—'}}</td><td>{{c.partial_directory_pages}}</td><td>{{c.last_error||(c.source==='NOT_IMPLEMENTED'?'尚未实现':c.enabled?'自动发现中':'默认关闭')}}</td></tr></tbody></table></div></section>
    <section class="panel"><h2>最近任务</h2><p v-if="!jobs.length" class="empty">暂无任务。</p><div class="table" v-else><table><thead><tr><th>集团</th><th>类型</th><th>状态</th><th>计划时间</th><th>错误</th></tr></thead><tbody><tr v-for="j in jobs" :key="j.id"><td>{{names[j.provider]}}</td><td>{{j.kind}}</td><td>{{j.status}}</td><td>{{when(j.scheduled_at)}}</td><td>{{j.error_type||'—'}}</td></tr></tbody></table></div></section>
    <footer>价格来自实际成功采集；无数据即显示空白 · Telegram 机器人可在本页面设置</footer>
  </main>
</template>
