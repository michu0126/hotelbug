<script setup>
defineProps({progress:{type:Object,default:null}});
</script>

<template>
  <section v-if="progress" class="panel" aria-label="IHG官网地图补采">
    <h2>IHG 官网地图补采</h2>
    <p>已读地图 {{progress.sitemaps_read}} / {{progress.sitemaps_total}} · 缺失 {{progress.sitemaps_missing}} · 失败待补采 {{progress.sitemaps_failed}}</p>
    <p>去重酒店候选 {{progress.hotel_candidates}} · 已经过候选位置 {{progress.candidate_positions_processed}}</p>
    <p class="muted">与地区目录自动轮换。已知酒店和已有任务会跳过；候选位置不是成功收录数，也不是全年报价覆盖数。</p>
    <p v-if="progress.last_error" role="status">地图问题：{{progress.last_error}}</p>
  </section>
</template>
