<script setup>
import {offerTags} from './offerPolicy.js';
defineProps({alerts:{type:Array,required:true},groupNames:{type:Object,required:true}});
const verificationLabel=a=>a.confirmed?'已确认':({REJECTED:'报价未通过复查',FAILED:'复查失败',CANCELLED:'复查已取消',PENDING:'待复查'}[a.payload.verification]||'复查状态未确认');
const verificationReason=a=>a.payload.verification_reason==='EXCLUDED_OFFER'?`特殊方案不作普通现金价异常提醒：${offerTags(a.payload.offer_classification)}`:({PROVIDER_DISABLED:'集团采集已关闭',OUTSIDE_MONITORING_WINDOW:'住宿已超出监测日期范围'}[a.payload.verification_reason]||a.payload.verification_error||'');
const eventLabel=a=>a.event_type==='NEW_HISTORICAL_LOW'?'历史新低':a.payload.new_historical_low?'降价 · 同时为历史新低':'降价';
const scoreLevel=v=>({NORMAL:'普通',LOW_PRICE:'低价',VERY_LOW_PRICE:'极低价',POSSIBLE_BUG_PRICE:'疑似 Bug 价'}[v]||'');
const featureLabel=v=>({drop_20:'降幅超过20%',drop_30:'降幅超过30%',drop_40:'降幅超过40%',drop_50:'降幅超过50%',drop_70:'降幅超过70%',neighbor:'低于前后日期',near_low:'接近历史最低',new_low:'刷新历史最低',verified:'独立复查通过',consistent_history:'多个历史窗口一致'}[v]||v);
const comparisons=[['previous_price','前次报价'],['median_7d','7天中位数'],['median_30d','30天中位数'],['median_90d','90天中位数'],['neighbor_date_median','前后日期'],['same_weekday_median','同星期日期'],['month_mean','本月均价']];
</script>
<template>
  <section class="panel">
    <h2>降价与历史新低提醒</h2>
    <p class="muted">历史新低不等于 Bug 价；二者都需独立复查。评分仅解释与排序，没有邻日等证据不补分。</p>
    <p v-if="!alerts.length" class="empty">暂无价格事件。积累历史报价后自动比较并复查。</p>
    <div v-else class="table"><table>
      <thead><tr><th>酒店 / 集团</th><th>入住日期</th><th>当前价格</th><th>历史基准</th><th>类型 / 评分</th><th>复查结果</th></tr></thead>
      <tbody><tr v-for="a in alerts" :key="a.id">
        <td>{{a.payload.hotel_name}} · {{groupNames[a.payload.provider]}}</td><td>{{a.payload.check_in}}</td>
        <td>{{a.payload.price}} {{a.payload.currency}}<small v-if="a.payload.price_field==='cash_price'"> · 官网展示价，全部税费未确认</small></td>
        <td>{{a.payload.baseline}} {{a.payload.currency}}<small v-if="a.payload.baseline_window_days"> · {{a.payload.baseline_window_days}} 日中位数</small><small v-else-if="a.payload.baseline_kind==='PREVIOUS_HISTORICAL_LOW'"> · 此前历史最低基准</small><small v-if="a.payload.historical_low"><br>此前历史最低 {{a.payload.historical_low}}</small></td>
        <td>{{eventLabel(a)}}<small v-if="a.payload.anomaly_level"><br>{{a.anomaly_score}} / 100 · {{scoreLevel(a.payload.anomaly_level)}}</small>
          <details v-if="a.payload.score_breakdown"><summary>评分依据</summary><div v-for="(weight,feature) in a.payload.score_breakdown" :key="feature">{{featureLabel(feature)}}：{{weight}}</div>
            <template v-if="a.payload.analysis"><div v-for="[key,label] in comparisons" :key="key"><small v-if="a.payload.analysis[`drop_from_${key}_percent`]!=null">较{{label}}：{{a.payload.analysis[`drop_from_${key}_percent`]}}%</small></div></template>
          </details>
        </td>
        <td>{{verificationLabel(a)}}<small v-if="verificationReason(a)"> · {{verificationReason(a)}}</small></td>
      </tr></tbody>
    </table></div>
  </section>
</template>
