export const dashboardTabs=[
  {id:'overview',label:'采集概览'},
  {id:'calendar',label:'价格日历'},
  {id:'notifications',label:'降价通知'},
  {id:'settings',label:'设置'},
];
export function collectionStatus(provider){
  if(!provider?.enabled)return {label:'未启用',tone:'neutral'};
  return ({ONLINE:{label:'正常采集',tone:'good'},BLOCKED:{label:'官网暂限访问',tone:'warning'},
    DEGRADED:{label:'待检查',tone:'warning'},BROKEN:{label:'采集异常',tone:'warning'}})[provider.status]
    ||{label:'等待采集',tone:'neutral'};
}
