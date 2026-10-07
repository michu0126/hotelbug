import test from 'node:test';
import assert from 'node:assert/strict';
import {catalogCoverage} from './catalogCoverage.js';

test('regional homepage fallback is visible as failed and redirected, never complete', () => {
  const text = catalogCoverage({source:'OFFICIAL_BROWSER_DIRECTORY',catalog_coverage_status:'FAILED_PAGES',reachable_failed_directory_pages:1,redirected_directory_pages:1});
  assert.equal(text, '有页面失败 · 失败 1 页，官网回退 1 页');
  assert.ok(!text.includes('目录全量已核对'));
});

test('legacy catalogs without the new field retain pending and incomplete reasons', () => {
  assert.equal(catalogCoverage({source:'OFFICIAL_BROWSER_DIRECTORY',catalog_coverage_status:'IN_PROGRESS',pending_directory_pages:3,unverified_directory_pages:2}), '遍历中 · 待读取 3 页，未核对 2 页');
});

test('a fully verified graph still needs its explicit coverage status', () => {
  assert.equal(catalogCoverage({source:'OFFICIAL_BROWSER_DIRECTORY',catalog_coverage_status:'FULL_CATALOG_VERIFIED',redirected_directory_pages:0}), '目录全量已核对');
  assert.equal(catalogCoverage({source:'OFFICIAL_BROWSER_DIRECTORY',catalog_coverage_status:'UNRECOGNIZED'}), '完整性未确认');
});

test('sitemap and unsupported source explanations remain independent', () => {
  assert.equal(catalogCoverage({source:'OFFICIAL_SITEMAP',sitemaps_missing:2,sitemaps_failed:1,sitemap_index_error:true}), '2 个地图缺失，1 个失败待补采，根索引刷新失败；全球酒店总数未核对');
  assert.equal(catalogCoverage({source:'NOT_IMPLEMENTED'}), '尚未实现');
});
