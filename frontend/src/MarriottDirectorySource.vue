<script setup>
import {computed,ref,watch} from 'vue';
import {requestDirectorySource,directorySourceLabel,directoryRequestStatus} from './marriottDirectorySource.js';

const props=defineProps({catalog:{type:Object,default:null},adminToken:{type:String,default:''},request:{type:Function,required:true}});
const emit=defineEmits(['changed']);
const selection=ref(''),busy=ref(false),message=ref('');
const choices=computed(()=>props.catalog?.directory_root_choices||[]);
watch(()=>props.catalog?.selected_directory_root,root=>{
  if(!selection.value) selection.value=root||choices.value[0]?.url||'';
},{immediate:true});
async function submit(){
  busy.value=true;message.value='';
  try{
    const result=await requestDirectorySource(props.request,props.adminToken,selection.value,choices.value);
    message.value=`目录来源核对任务已提交：${result.id}。正常读取成功后才采用；未成功时保留当前来源。`;
    emit('changed');
  }catch(error){message.value=error.message;}finally{busy.value=false;}
}
</script>

<template>
  <section v-if="catalog" class="panel" aria-label="万豪全球目录来源">
    <h2>万豪全球目录来源</h2>
    <p>当前采用：{{directorySourceLabel(catalog.selected_directory_root,choices)}}</p>
    <p v-if="catalog.requested_directory_root">最近申请：{{directorySourceLabel(catalog.requested_directory_root,choices)}} · {{directoryRequestStatus(catalog)}}<span v-if="catalog.requested_directory_root_error"> · {{catalog.requested_directory_root_error}}</span></p>
    <p class="muted">中文和英文官网均为全球目录入口。选定来源正常读取成功后，后台沿该来源继续自动发现酒店；目录来源切换不代表全年报价已抓取完成。保留已有酒店、历史价格与任务，遵守当前集团暂停和请求间隔。</p>
    <div class="fields">
      <label>核对并采用的目录来源<select v-model="selection" :disabled="busy||!choices.length"><option v-for="choice in choices" :key="choice.url" :value="choice.url">{{choice.label}}</option></select></label>
      <button class="primary" :disabled="busy||!adminToken||!catalog.enabled||!choices.length" @click="submit">{{busy?'提交中…':'核对并采用此来源'}}</button>
    </div>
    <p v-if="!choices.length" class="muted">当前后台尚未提供目录来源选项，请更新到支持来源选择的版本。</p>
    <p v-else-if="!catalog.enabled" class="muted">请先在自动采集设置中启用万豪与全球轮询；此按钮不会修改启用设置或解除暂停。</p>
    <p v-else-if="!adminToken" class="muted">填写上方管理令牌后可提交来源核对任务。</p>
    <p v-if="message" role="status" class="notice">{{message}}</p>
  </section>
</template>
