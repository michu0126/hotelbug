<script setup>
import {countLabel,priceCoveragePercent} from './priceCoverage.js';
defineProps({catalogs:{type:Array,default:()=>[]},names:{type:Object,required:true}});
</script>

<template>
  <section class="panel" aria-label="未来一年价格采集进度">
    <h2>未来一年价格采集进度</h2>
    <p class="muted">范围为当前价格来源下已收录且启用的酒店，排除已识别的测试／培训条目，未来 365 个入住日，每次一晚、1 间房、2 成人；不是全球酒店总量。房型报价按入住日确认；万豪中文官网另支持按月读取日历起价，独立统计于下表，不代表一次取得全年可订房型。</p>
    <p class="muted">“更新有效”表示最近完成的查询仍在设置的刷新间隔内，包括明确无房或未开放预订的日期。历史报价和排队任务不会被算作正在有效监控；切换官网来源后，无法核对来源的旧记录也不会计入。</p>
    <div class="table"><table>
      <thead><tr><th>集团</th><th>范围内酒店</th><th>更新有效 / 应查询日期</th><th>有效覆盖率</th><th>已过期</th><th>尚未查询</th><th>有效查询结果</th></tr></thead>
      <tbody><tr v-for="c in catalogs" :key="c.provider">
        <td>{{names[c.provider]}}</td>
        <template v-if="c.price_coverage">
          <td>{{countLabel(c.price_coverage.eligible_hotels)}}<div v-if="c.price_coverage.excluded_test_hotels" class="muted">另排除测试／培训酒店 {{countLabel(c.price_coverage.excluded_test_hotels)}} 家（目录及历史保留）</div></td>
          <td>{{countLabel(c.price_coverage.fresh_stays)}} / {{countLabel(c.price_coverage.required_stays)}}</td>
          <td>{{priceCoveragePercent(c.price_coverage)}}</td>
          <td>{{countLabel(c.price_coverage.stale_stays)}}</td>
          <td>{{countLabel(c.price_coverage.unscanned_stays)}}<div v-if="c.price_coverage.source_unverified_stays" class="muted">其中旧来源或来源待核对 {{countLabel(c.price_coverage.source_unverified_stays)}}</div></td>
          <td>有报价 {{countLabel(c.price_coverage.fresh_available_stays)}}<br>无房 {{countLabel(c.price_coverage.fresh_unavailable_stays)}} · 未开放 {{countLabel(c.price_coverage.fresh_not_open_stays)}}</td>
        </template>
        <td v-else colspan="6" class="muted">当前后台尚未提供真实日期覆盖统计</td>
      </tr></tbody>
    </table></div>
    <p v-for="c in catalogs.filter(c=>c.price_coverage?.refresh_demand_exceeds_start_ceiling)" :key="`capacity-${c.provider}`" class="notice">
      {{names[c.provider]}}：按逐日确认全部房型报价计算，当前刷新需求超过请求间隔的理论上限；实际还需加上网页加载时间。需要多日批量采集能力；日历起价可辅助筛选降价日期，但不会计入上面的房型报价覆盖。
    </p>
    <h3>日历起价观察（与房型报价分开）</h3>
    <p class="muted">读取官网月历中的每日最低展示价。税费是否包含、是否要求会员均未确认，不能视为最终含税房价；重复抓取同一家酒店同一天只计一次。只有完成房型报价复核后才可能发送降价通知。</p>
    <div class="table"><table>
      <thead><tr><th>集团</th><th>日历采集状态</th><th>有观察的酒店 / 范围内酒店</th><th>新鲜日期 / 已观察日期</th><th>已过期日期</th></tr></thead>
      <tbody><tr v-for="c in catalogs" :key="`hints-${c.provider}`">
        <td>{{names[c.provider]}}</td>
        <template v-if="c.calendar_hints?.supported">
          <td>按月日历起价 · 未计入房型覆盖</td>
          <td>{{countLabel(c.calendar_hints.observed_hotels)}} / {{countLabel(c.calendar_hints.eligible_hotels)}}</td>
          <td>{{countLabel(c.calendar_hints.fresh_dates)}} / {{countLabel(c.calendar_hints.observed_dates)}}</td>
          <td>{{countLabel(c.calendar_hints.stale_dates)}}</td>
        </template>
        <td v-else colspan="4" class="muted">{{!c.calendar_hints ? '当前后台尚未提供日历起价统计' : c.calendar_hints.status === 'CN_SOURCE_NOT_SELECTED' ? '尚未采纳万豪中文官网来源，日历起价不计数' : '该集团暂未接入按月日历起价'}}</td>
      </tr></tbody>
    </table></div>
  </section>
</template>
