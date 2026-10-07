<script setup>
import {computed,ref,watch} from 'vue';
import {historyChart} from './historyChart.js';
import {offerPolicyText} from './offerPolicy.js';

const props=defineProps({hotelId:String,preferredOffer:String});
const checkIn=defineModel('checkIn',{default:''});
const offers=ref([]),offerKey=ref(''),cursor=ref(null);
const days=ref(30),trend=ref(null),error=ref(''),offersLoading=ref(false),trendLoading=ref(false);
let offersSequence=0,trendSequence=0;
const chart=computed(()=>historyChart(trend.value));
const offer=computed(()=>trend.value?.offer);
const money=v=>v==null?'—':String(v).replace(/(\.\d*?[1-9])0+$|\.0+$/, '$1');
const flag=(v,yes,no)=>v==null?'未确认':v?yes:no;
const basis=v=>v==='nightly'?'每晚':v==='stay_total'?'整段住宿':v;
const when=v=>v?new Date(v).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}):'—';
const optionLabel=o=>`${o.room_type||o.room_code||'房型未确认'} · ${o.rate_name||o.rate_code||'方案未确认'}${o.rate_code?' #'+o.rate_code:''} · ${o.currency} · ${o.adults}成人/${o.rooms}房 · 退房${o.check_out} · ${basis(o.price_basis)} · ${o.price_field==='total_price'?'含税费':'展示价'} · ${flag(o.member_rate,'会员','非会员')} · 取消${flag(o.refundable,'可退','不可退')} · 早餐${flag(o.breakfast_included,'含','不含')}`;
async function get(path){
  const response=await fetch('/api'+path,{signal:AbortSignal.timeout(15000)});
  if(!response.ok)throw new Error(`查询失败：HTTP ${response.status}`);
  return response.json();
}
async function loadOffers(more=false){
  const sequence=++offersSequence,hotel=props.hotelId,date=checkIn.value;
  if(!more){offers.value=[];cursor.value=null;offerKey.value='';trend.value=null;++trendSequence;}
  error.value='';
  if(!hotel||!date){offersLoading.value=false;return;}
  offersLoading.value=true;
  try{
    const result=await get(`/hotels/${encodeURIComponent(hotel)}/offers?check_in=${encodeURIComponent(date)}${more&&cursor.value?'&cursor='+encodeURIComponent(cursor.value):''}`);
    if(sequence!==offersSequence||hotel!==props.hotelId||date!==checkIn.value)return;
    const merged=[...offers.value,...result.offers];
    offers.value=[...new Map(merged.map(o=>[o.offer_key,o])).values()];cursor.value=result.next_cursor;
    if(!offerKey.value)offerKey.value=offers.value.find(o=>o.offer_key===props.preferredOffer)?.offer_key||offers.value[0]?.offer_key||'';
  }catch(e){if(sequence===offersSequence)error.value=e.message;}
  finally{if(sequence===offersSequence)offersLoading.value=false;}
}
async function loadTrend(){
  const sequence=++trendSequence,hotel=props.hotelId,key=offerKey.value,windowDays=days.value;
  trend.value=null;error.value='';
  if(!hotel||!key){trendLoading.value=false;return;}
  trendLoading.value=true;
  try{
    const result=await get(`/hotels/${encodeURIComponent(hotel)}/trend?offer_key=${encodeURIComponent(key)}&days=${windowDays}`);
    if(sequence===trendSequence&&hotel===props.hotelId&&key===offerKey.value&&windowDays===days.value)trend.value=result;
  }catch(e){if(sequence===trendSequence)error.value=e.message;}
  finally{if(sequence===trendSequence)trendLoading.value=false;}
}
watch(()=>props.preferredOffer,value=>{if(value&&offers.value.some(o=>o.offer_key===value))offerKey.value=value;});
watch([()=>props.hotelId,checkIn],()=>loadOffers(),{immediate:true});
watch([offerKey,days],loadTrend);
</script>
<template>
  <section class="panel history-panel" aria-labelledby="history-heading">
    <h2 id="history-heading">同条件历史价格</h2>
    <p class="muted">选择入住日期及具体房型／方案。不同币种、人数、晚数、税费口径和会员条件分开查看；不把历史观察价当作当前可订价。</p>
    <p v-if="!hotelId" class="empty">先选择一间已收录酒店。</p>
    <template v-else>
      <div class="fields">
        <label>历史查询入住日期<input v-model="checkIn" type="date"></label>
        <label class="history-offer">历史报价条件<select v-model="offerKey" :disabled="!offers.length||offersLoading"><option v-if="!offers.length" value="">暂无历史报价</option><option v-for="o in offers" :key="o.offer_key" :value="o.offer_key">{{optionLabel(o)}}</option></select></label>
        <label>观察时间范围<select v-model.number="days"><option :value="1">最近24小时</option><option :value="7">最近7天</option><option :value="30">最近30天</option><option :value="90">最近90天</option></select></label>
        <button v-if="cursor" :disabled="offersLoading" @click="loadOffers(true)">加载更多方案</button>
      </div>
      <p v-if="offersLoading||trendLoading" role="status" class="muted">正在读取历史记录…</p>
      <p v-if="error" role="alert" class="notice error">{{error}}</p>
      <p v-else-if="!offersLoading&&!offers.length" class="empty">这个入住日期尚无历史报价，不会自动填入示例价格。</p>
      <template v-if="trend">
        <p class="history-conditions">{{offer.check_in}} → {{offer.check_out}} · {{offer.adults}}成人／{{offer.rooms}}房 · {{offer.currency}} · {{basis(offer.price_basis)}} · {{offer.price_field==='total_price'?'含税费':'官网展示价，全部税费未确认'}}<br>{{flag(offer.member_rate,'会员价','公开非会员价')}} · 取消：{{flag(offer.refundable,'可退款','不可退款')}} · 早餐：{{flag(offer.breakfast_included,'包含','不包含')}}</p>
        <p class="notice" role="status">{{offerPolicyText(offer.offer_classification)}}</p>
        <div class="history-stats">
          <article><small>窗口内最后观察价</small><strong>{{money(trend.last_observed_price)}} {{offer.currency}}</strong><small>{{when(trend.last_observed_at)}}</small></article>
          <article><small>窗口最低／最高</small><strong>{{money(trend.window_low)}} / {{money(trend.window_high)}}</strong><small>{{offer.currency}} · {{days}}天观察窗口</small></article>
          <article><small>同条件历史最低</small><strong>{{money(trend.historical_low)}} {{offer.currency}}</strong><small>所有已保存的有效观察</small></article>
          <article><small>有效观察</small><strong>{{trend.observation_count}} 次</strong><small>覆盖 {{trend.observation_days}} 个UTC日期</small></article>
        </div>
        <figure v-if="chart" class="history-figure">
          <svg viewBox="0 0 640 240" role="img" :aria-label="`${trend.hotel_name}同条件${days}天每日最低与最高报价趋势`">
            <g v-for="tick in chart.ticks" :key="tick.y"><line x1="65" x2="620" :y1="tick.y" :y2="tick.y" class="history-grid"/><text x="57" :y="tick.y+4" text-anchor="end">{{Number(tick.value.toPrecision(5))}}</text></g>
            <g v-for="(segment,i) in chart.segments" :key="i"><polyline :points="segment.high" class="history-high-line"/><polyline :points="segment.low" class="history-low-line"/></g>
            <g v-for="p in chart.points" :key="p.date"><title>{{p.date}} UTC · {{offer.currency}} {{money(p.low)}}–{{money(p.high)}} · {{p.samples}}次观察</title><line :x1="p.x" :x2="p.x" :y1="p.yLow" :y2="p.yHigh" class="history-range"/><circle :cx="p.x" :cy="p.yHigh" r="3" class="history-high-dot"/><circle :cx="p.x" :cy="p.yLow" r="3" class="history-low-dot"/></g>
            <text x="65" y="218">{{chart.startLabel}}</text><text x="620" y="218" text-anchor="end">{{chart.endLabel}}</text>
          </svg>
          <figcaption><span class="history-low-key">● 每日最低</span> <span class="history-high-key">● 每日最高</span> · UTC观察日期；缺失日期不补价、不连线。此图不是异常检测中位数基线。</figcaption>
        </figure>
        <p v-else class="empty">所选时间范围内没有有效现金报价。历史最低仍按全部已保存记录计算。</p>
        <details v-if="trend.points.length"><summary>查看逐日记录（{{trend.points.length}}天）</summary><div class="table"><table><thead><tr><th>观察日期（UTC）</th><th>最低</th><th>最高</th><th>均值</th><th>次数</th></tr></thead><tbody><tr v-for="p in trend.points" :key="p.date"><td>{{p.date}}</td><td>{{money(p.low)}} {{offer.currency}}</td><td>{{money(p.high)}} {{offer.currency}}</td><td>{{money(p.mean)}} {{offer.currency}}</td><td>{{p.samples}}</td></tr></tbody></table></div></details>
      </template>
    </template>
  </section>
</template>
