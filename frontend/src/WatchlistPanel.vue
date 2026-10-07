<script setup>
import {onMounted,onUnmounted,ref,watch} from 'vue';
import {scopeNames,watchPayload,watchTarget} from './watchForm.js';
const props=defineProps({adminToken:{type:String,default:''},groupNames:{type:Object,required:true},request:{type:Function,required:true},revision:{type:Number,default:0}});
const emit=defineEmits(['changed']);
const scope=ref('provider'),target=ref(''),group=ref('marriott'),days=ref(365),name=ref('');
const rows=ref([]),targets=ref([]),offset=ref(0),more=ref(false),loading=ref(false),busy=ref(false),message=ref('');
const pageSize=50;
let debounce,requestVersion=0,listVersion=0;
function headers(){return {'content-type':'application/json','authorization':`Bearer ${props.adminToken}`};}
async function loadList(){
  const version=++listVersion,position=offset.value;
  loading.value=true;
  try{
    const values=await props.request(`/watchlists?limit=${pageSize+1}&offset=${position}`);
    if(version!==listVersion)return;
    rows.value=values.slice(0,pageSize);more.value=values.length>pageSize;
  }catch(e){if(version===listVersion)message.value=e.message;}
  finally{if(version===listVersion)loading.value=false;}
}
async function loadTargets(){
  const version=++requestVersion;
  if(scope.value==='provider'){targets.value=[];return;}
  try{
    const params=new URLSearchParams({scope:scope.value,q:target.value,limit:'100'});
    if(group.value)params.set('provider',group.value);
    const values=await props.request('/watchlists/targets?'+params);
    if(version===requestVersion)targets.value=values;
  }catch(e){if(version===requestVersion)message.value=e.message;}
}
async function save(){
  if(!props.adminToken){message.value='请先填写上方管理令牌';return;}
  busy.value=true;message.value='';
  try{
    const body=watchPayload(scope.value,scope.value==='provider'?group.value:target.value,group.value,days.value,name.value);
    await props.request('/watchlists',{method:'POST',headers:headers(),body:JSON.stringify(body)});
    message.value='范围监控已保存。匹配的已收录酒店及以后新收录酒店将进入优先监控；关闭的集团不会采价。';
    offset.value=0;await loadList();emit('changed');
  }catch(e){message.value=e.message;}finally{busy.value=false;}
}
async function toggle(row){
  if(!props.adminToken){message.value='请先填写上方管理令牌';return;}
  busy.value=true;message.value='';
  try{
    await props.request('/watchlists/'+encodeURIComponent(row.id),{method:'PATCH',headers:headers(),body:JSON.stringify({enabled:!row.enabled})});
    message.value=row.enabled?'已暂停此范围的新任务生成；已排队任务和全球轮询不受影响。':'已恢复范围监控，继续已有日期进度。';
    await loadList();emit('changed');
  }catch(e){message.value=e.message;}finally{busy.value=false;}
}
function changePage(delta){offset.value=Math.max(0,offset.value+delta*pageSize);loadList();}
watch([scope,group],()=>{if(scope.value==='provider'&&!group.value)group.value=Object.keys(props.groupNames)[0]||'';target.value='';targets.value=[];loadTargets();});
watch(target,()=>{clearTimeout(debounce);requestVersion++;debounce=setTimeout(loadTargets,250);});
watch(()=>props.revision,loadList);
watch(()=>props.adminToken,()=>{message.value='';});
onMounted(()=>{loadList();loadTargets();});
onUnmounted(()=>{clearTimeout(debounce);requestVersion++;listVersion++;});
</script>
<template>
  <section class="panel watchlist-panel" aria-labelledby="watchlist-heading">
    <h2 id="watchlist-heading">关注监控范围</h2>
    <p class="muted">可关注单酒店、城市、国家、品牌或集团。这里为酒店库中匹配的酒店优先生成逐日任务，不代表已完成官网全球目录；新增匹配酒店会自动纳入。单酒店可在上方保存。</p>
    <div class="fields">
      <label>范围类型<select v-model="scope"><option value="provider">酒店集团</option><option value="city">城市</option><option value="country">国家</option><option value="brand">品牌</option></select></label>
      <label>{{scope==='provider'?'关注集团':'限定集团'}}<select v-model="group"><option v-if="scope!=='provider'" value="">所有集团</option><option v-for="(label,key) in groupNames" :key="key" :value="key">{{label}}</option></select></label>
      <label v-if="scope!=='provider'">{{scopeNames[scope]}}目标<input v-model="target" list="watch-target-values" maxlength="100" placeholder="输入关键词，从已有酒店资料中选择"><datalist id="watch-target-values"><option v-for="value in targets" :key="value" :value="value"/></datalist></label>
      <label>范围监控天数<select v-model="days"><option :value="30">30 天</option><option :value="90">90 天</option><option :value="365">365 天</option></select></label>
      <label>范围名称（可选）<input v-model="name" maxlength="200" placeholder="用于识别此监控范围"></label>
      <button class="primary" :disabled="busy||!adminToken" @click="save">保存范围监控</button>
    </div>
    <p v-if="scope!=='provider'" class="muted">按酒店资料中的完整值匹配，不自动翻译国家／城市或猜测品牌。候选来自酒店库；未知资料保持空值，不匹配此范围。尚无酒店的目标可以保存，待以后收录。</p>
    <p v-if="message" role="status" class="notice">{{message}}</p>
    <p class="muted">范围优先级高于普通轮询，仍遵守集团开关、暂停、限流和总积压上限。暂停范围仅停止新增任务，不取消已排队任务，也不关闭全球轮询。</p>
    <div class="table"><table><thead><tr><th>名称</th><th>范围 / 目标</th><th>未来天数</th><th>状态</th><th>操作</th></tr></thead><tbody>
      <tr v-for="row in rows" :key="row.id"><td>{{row.name}}</td><td>{{scopeNames[row.scope]||row.scope}} · {{watchTarget(row,groupNames)}}<small v-if="row.scope!=='provider'&&row.filters.provider"> · {{groupNames[row.filters.provider]||row.filters.provider}}</small></td><td>{{row.filters.days_ahead||30}}</td><td>{{row.enabled?'已启用':'已暂停'}}</td><td><button :disabled="busy||!adminToken" @click="toggle(row)">{{row.enabled?'暂停范围':'恢复范围'}}</button></td></tr>
    </tbody></table></div>
    <p v-if="!rows.length&&!loading" class="empty">尚无关注监控。全球轮询开关与此列表独立。</p>
    <div class="fields"><button :disabled="loading||offset===0" @click="changePage(-1)">上一页范围</button><span>第 {{Math.floor(offset/pageSize)+1}} 页，本页 {{rows.length}} 项</span><button :disabled="loading||!more" @click="changePage(1)">下一页范围</button><button :disabled="loading" @click="loadList">刷新范围列表</button></div>
  </section>
</template>
