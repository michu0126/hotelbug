<script setup>
import {computed,ref,watch} from 'vue';
const props=defineProps({adminToken:{type:String,default:''},request:{type:Function,required:true},hotels:{type:Array,default:()=>[]},groupNames:{type:Object,required:true}});
const emit=defineEmits(['changed']);
const provider=ref('marriott'),rows=ref([]),cookieText=ref(''),origin=ref('https://www.marriott.com'),message=ref(''),busy=ref(false),showCookie=ref(false),hotelId=ref('');
const defaults={marriott:'https://www.marriott.com',ihg:'https://www.ihg.com',hilton:'https://www.hilton.com',hyatt:'https://www.hyatt.com',accor:'https://all.accor.com',gha:'https://www.ghadiscovery.com'};
const tomorrow=new Date(Date.now()+86400000);
const checkIn=ref(`${tomorrow.getFullYear()}-${String(tomorrow.getMonth()+1).padStart(2,'0')}-${String(tomorrow.getDate()).padStart(2,'0')}`);
const connection=computed(()=>rows.value.find(row=>row.provider===provider.value));
const choices=computed(()=>props.hotels.filter(h=>h.provider===provider.value&&h.active!==false));
const labels={SAVED_NOT_VERIFIED:'已保存 · 尚未验证报价',EXPIRED:'Cookie 已过期',NOT_CONFIGURED:'未配置',CONFIG_ERROR:'配置无法读取，请重新保存'};
const headers=()=>({'content-type':'application/json',authorization:`Bearer ${props.adminToken}`});
function replace(row){rows.value=[...rows.value.filter(item=>item.provider!==row.provider),row];}
async function load(){
  if(!props.adminToken){message.value='请先填写上方管理令牌。';return;}
  busy.value=true;message.value='';const token=props.adminToken;
  try{const result=await props.request('/settings/connections',{headers:headers()});if(token!==props.adminToken)return;rows.value=result;origin.value=connection.value?.origin||defaults[provider.value];}
  catch(error){message.value=error.message;}finally{busy.value=false;}
}
async function save(enabled=true){
  if(!props.adminToken)return;
  busy.value=true;message.value='';const token=props.adminToken;
  try{
    const result=await props.request(`/settings/connections/${provider.value}`,{method:'PUT',headers:headers(),body:JSON.stringify({origin:origin.value.trim(),enabled,cookie_text:enabled?cookieText.value:undefined})});
    cookieText.value='';showCookie.value=false;if(token!==props.adminToken)return;replace(result);
    message.value=enabled?`已保存 ${result.cookie_count} 个 Cookie。后续新采集任务自动使用；这不代表登录或报价验证成功。`:'已清除导入配置；后续新任务会移除相应 Cookie，不中断正在执行的请求。';
  }catch(error){message.value=error.message;}finally{busy.value=false;}
}
async function test(){
  busy.value=true;message.value='';const token=props.adminToken;
  try{
    const result=await props.request(`/settings/connections/${provider.value}/test`,{method:'POST',headers:headers(),body:JSON.stringify({hotel_id:hotelId.value,check_in:checkIn.value})});
    if(token!==props.adminToken)return;replace(result);
    message.value=`真实报价任务已提交：${result.last_test.job_id}。条件为一晚、一间房、两名成人；请稍后读取连接状态。集团暂停时等待到期，不会强行解除。`;
    emit('changed');
  }catch(error){message.value=error.message;}finally{busy.value=false;}
}
function clear(){if(window.confirm('清除所选集团导入的 Cookie？不会删除酒店、价格历史或整个浏览器资料。'))save(false);}
watch(provider,()=>{origin.value=connection.value?.origin||defaults[provider.value];cookieText.value='';showCookie.value=false;hotelId.value='';message.value='';});
watch(()=>props.adminToken,()=>{rows.value=[];cookieText.value='';showCookie.value=false;message.value='';});
defineExpose({load});
</script>

<template>
  <section class="panel official-connections" aria-label="官网会话连接">
    <h2>官网会话连接</h2>
    <p class="muted">填写集团官网 Cookie，容器后续新任务自动使用。支持单行 Cookie 请求头、浏览器导出的 JSON 或 Netscape 文件内容；无需填写账号密码。仅在可信内网或 HTTPS 中使用。</p>
    <div class="fields">
      <label>酒店集团<select v-model="provider" :disabled="busy"><option v-for="(name,key) in groupNames" :key="key" :value="key">{{name}}</option></select></label>
      <label>Cookie 所属官网<input v-model="origin" type="url" :placeholder="defaults[provider]" :disabled="busy"></label>
      <button :disabled="busy||!adminToken" @click="load">读取连接状态</button>
    </div>
    <p>{{labels[connection?.status]||'尚未读取连接状态'}}<span v-if="connection?.cookie_count"> · {{connection.cookie_count}} 个未过期 Cookie</span></p>
    <p v-if="connection?.earliest_cookie_expiry" class="muted">最早 Cookie 到期：{{new Date(connection.earliest_cookie_expiry).toLocaleString('zh-CN')}}，不等同于账号登录到期。</p>
    <label>Cookie 内容（保存后不回显）<textarea v-model="cookieText" :class="{masked:!showCookie}" rows="4" autocomplete="off" spellcheck="false" :disabled="busy" placeholder="name=value; another=value，或粘贴 Cookie JSON / Netscape 内容"></textarea></label>
    <div class="fields"><label>显示本次输入<input v-model="showCookie" type="checkbox"></label><button class="primary" :disabled="busy||!adminToken||!cookieText.trim()" @click="save(true)">保存官网连接</button><button :disabled="busy||!adminToken||!connection?.configured" @click="clear">清除导入 Cookie</button></div>
    <p class="muted">Cookie 只保存在浏览器私有数据卷，不进入任务、SQL 设置或返回页面。原有采价、月历和全年调度继续工作；保存 Cookie 不会自动解决所有官网限制。万豪英文官网和中文官网 Cookie 不可混用。</p>
    <div class="fields">
      <label>验证酒店<select v-model="hotelId" :disabled="busy"><option value="">请选择该集团已收录酒店</option><option v-for="hotel in choices" :key="hotel.id" :value="hotel.id">{{hotel.hotel_name}}</option></select></label>
      <label>入住日期<input v-model="checkIn" type="date" :disabled="busy"></label>
      <button :disabled="busy||!adminToken||!connection?.configured||!hotelId||!checkIn||!!cookieText.trim()" @click="test">测试实际报价</button>
    </div>
    <p v-if="!choices.length" class="muted">当前酒店列表中没有该集团酒店；请先在上方搜索已收录酒店，或等待官网目录采集完成。</p>
    <p v-if="connection?.last_test">最近报价测试：{{connection.last_test.status}} · 已入库 {{connection.last_test.quote_count??0}} 条报价<span v-if="connection.last_test.error_type"> · {{connection.last_test.error_type}}</span>。任务成功也可能是无房／尚未开放预订，具体价格与会员条件以酒店日历为准。</p>
    <p v-if="connection?.blocked_until&&new Date(connection.blocked_until)>new Date()" class="muted">官网请求暂停至 {{new Date(connection.blocked_until).toLocaleString('zh-CN')}}。</p>
    <p v-if="message" role="status" class="notice">{{message}}</p>
  </section>
</template>

<style scoped>
textarea{width:100%;box-sizing:border-box;padding:12px;border:1px solid #d7e0e8;border-radius:8px;font:14px monospace;resize:vertical;display:block;margin-top:8px}.masked{-webkit-text-security:disc}
</style>
