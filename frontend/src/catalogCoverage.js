export function catalogCoverage(c) {
  if (c.source === 'OFFICIAL_SITEMAP') return `${c.sitemaps_missing} 个地图缺失，${c.sitemaps_failed || 0} 个失败待补采${c.sitemap_index_error ? '，根索引刷新失败' : ''}；全球酒店总数未核对`;
  if (c.source !== 'OFFICIAL_BROWSER_DIRECTORY') return '尚未实现';
  const labels = {NOT_STARTED:'尚未开始',IN_PROGRESS:'遍历中',FAILED_PAGES:'有页面失败',UNVERIFIED_PAGES:'有页面未完整核对',STALE_PAGES:'部分页面待更新',TRAVERSED_TOTAL_UNVERIFIED:'目录已遍历，全球总数未确认',COUNT_MISMATCH:'已遍历，酒店总数不一致',FULL_CATALOG_VERIFIED:'目录全量已核对'};
  const gaps = [['pending_directory_pages','待读取'],['reachable_failed_directory_pages','失败'],['redirected_directory_pages','官网回退'],['unverified_directory_pages','未核对'],['stale_directory_pages','待更新']].filter(([key]) => c[key] > 0).map(([key,label]) => `${label} ${c[key]} 页`);
  return `${labels[c.catalog_coverage_status] || '完整性未确认'}${gaps.length ? ' · ' + gaps.join('，') : ''}`;
}
