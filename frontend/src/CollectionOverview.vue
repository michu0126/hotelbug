<script setup>
import {collectionStatus} from './dashboardView.js';
import {priceCoveragePercent} from './priceCoverage.js';
defineProps({providers:{type:Array,default:()=>[]},catalogs:{type:Array,default:()=>[]},names:{type:Object,required:true}});
const emit=defineEmits(['settings']);
const catalog=(rows,name)=>rows.find(c=>c.provider===name);
</script>
<template>
  <section class="panel" aria-label="集团采集状态">
    <div class="section-heading"><div><h2>集团采集状态</h2><p class="muted">接口可用则用接口，其余使用官网 Cookie 会话。监测范围为未来一年。</p></div><button @click="emit('settings')">管理连接</button></div>
    <div class="collection-grid">
      <article v-for="p in providers" :key="p.provider" class="collection-card">
        <div class="section-heading"><h3>{{names[p.provider]||p.provider}}</h3><span class="status-pill" :class="collectionStatus(p).tone">{{collectionStatus(p).label}}</span></div>
        <p class="transport">{{p.collection_strategy?.label||'采集方式待后台确认'}}</p>
        <p class="muted">{{p.collection_strategy?.detail||'更新后台后可查看实际配置的采集通道。'}}</p>
        <div class="collection-counts"><span><strong>{{catalog(catalogs,p.provider)?.hotels??'—'}}</strong>已收录</span><span><strong>{{catalog(catalogs,p.provider)?.hotels_with_quotes??'—'}}</strong>曾取得报价</span><span><strong>{{priceCoveragePercent(catalog(catalogs,p.provider)?.price_coverage)}}</strong>有效日期覆盖</span></div>
        <small v-if="p.collection_strategy?.mode==='official_api_or_cookie'">英文接口已接入，当前环境报价仍需验证；中文官网仍走 Cookie。</small>
        <details v-if="p.last_error&&p.last_error!=='NOT_YET_RUN'"><summary>最近问题</summary><p class="muted">{{p.last_error}}</p></details>
      </article>
    </div>
    <p v-if="!providers.length" class="empty">正在读取采集状态…</p>
    <p class="muted footnote">已收录不等于已取得全年价格；覆盖率仅统计最近有效的日期查询，不将失败或缺失价格记为零。</p>
  </section>
</template>
