<script setup>
const props=defineProps({policy:{type:Object,required:true}});
const emit=defineEmits(['update:policy']);
const fields=[
  ['drop_20','下降超过 20% 权重',0,100],['drop_30','下降超过 30% 权重',0,100],
  ['drop_40','下降超过 40% 权重',0,100],['drop_50','下降超过 50% 权重',0,100],
  ['drop_70','下降超过 70% 权重',0,100],['neighbor','低于前后日期权重',0,100],
  ['near_low','接近历史低价权重',0,100],['new_low','刷新历史最低权重',0,100],
  ['verified','独立复查通过权重',0,100],['consistent_history','多个历史窗口一致权重',0,100],
  ['low_threshold','低价等级起点',1,100],['very_low_threshold','极低价等级起点',1,100],
  ['possible_bug_threshold','疑似 Bug 价等级起点',1,100],
  ['support_min_days','一致性评分至少历史天数',3,90],
  ['calendar_max_age_hours','相邻日期报价有效小时',1,168],
  ['neighbor_drop_fraction','低于邻日的降幅（%）',1,99,true],
  ['near_low_margin','接近历史最低允许高出（%）',0,99,true],
];
const value=(key,percent)=>Number(props.policy[key])*(percent?100:1);
function update(key,percent,event){emit('update:policy',{...props.policy,[key]:Number(event.target.value)/(percent?100:1)});}
</script>
<template>
  <details v-if="policy">
    <summary>异常评分高级设置</summary>
    <p class="muted">只用于解释和排序，不额外阻拦达到降价阈值的提醒。降幅档只取一档，历史新低与接近历史低价互斥；没有邻日证据不加分。等级起点必须从低到高递增。复查使用发现候选时的评分设置。</p>
    <div class="fields"><label v-for="[key,label,min,max,percent] in fields" :key="key">{{label}}<input :value="value(key,percent)" type="number" :min="min" :max="max" step="1" @input="update(key,percent,$event)"></label></div>
  </details>
</template>
