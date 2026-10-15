export function directorySourceJob(root, choices) {
  if (!Array.isArray(choices) || !choices.some(choice => choice.url === root)) {
    throw new Error('请选择后台提供的万豪全球目录来源');
  }
  return {provider:'marriott',kind:'DISCOVER_CATALOG',priority:70,payload:{official_url:root}};
}

export function requestDirectorySource(request, adminToken, root, choices) {
  return request('/jobs',{method:'POST',headers:{'content-type':'application/json',authorization:`Bearer ${adminToken}`},
    body:JSON.stringify(directorySourceJob(root,choices))});
}

export function directorySourceLabel(root, choices=[]) {
  return choices.find(choice => choice.url === root)?.label || (root ? '来源未识别' : '尚未确认');
}

export function directoryRequestStatus(catalog) {
  if (!catalog?.requested_directory_root) return '尚无切换申请';
  const status=catalog.requested_directory_root_status;
  if (catalog.requested_directory_root === catalog.selected_directory_root && status === 'SUCCEEDED') {
    return '已验证采用';
  }
  return {PENDING:'待调度',QUEUED:'已入队',RUNNING:'读取中',FAILED:'读取失败，保留当前来源',
    CANCELLED:'已取消，保留当前来源',SUCCEEDED:'尚未采用'}[status] || '尚未取得任务结果';
}
