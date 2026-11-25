import {api, download} from '../api/client.js';
import {empty, errorSlot} from '../components/common/layout.js';
import {table} from '../components/common/table.js';
import {actions,kinds} from '../constants/enums.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {esc,time} from '../utils/html.js';

export async function overview() {
  const profile = state.profile;
  const history = await api('/audit');
  return `<section class="summary"><h2>${esc(profile.organization)}</h2><p>${esc(profile.context)}</p><ol>${profile.scenario.map(step => `<li>${esc(step)}</li>`).join('')}</ol><a class="button" href="#1">核对数据</a></section><section><h2>最近操作</h2>${table(history.map(row => ({时间:time(row.at),账号:row.actor,操作:actions[row.action] ?? row.action,记录:row.subject})))}</section>`;
}

function versionLabel(row) {
  const kind = kinds[row.kind] ?? '数据版本';
  const version = row.version ? ` v${row.version}` : '';
  const active = row.is_active_version ? ' · 当前生效' : row.superseded ? ' · 已被新版取代' : row.pending ? ' · 待生效' : '';
  return `${kind}${version} · ${row.name} · #${row.id}${active}`;
}

function mergeBox(report) {
  if (!report) return '';
  const conflicts = report.conflicts || [];
  const unresolved = report.unresolved_pairs || [];
  const summary = `<p class="hint">补报 ${report.supplement_files} 份 · 计划窗口 ${report.planned_windows} · 原始已报 ${report.base_windows} · 本次填补 ${report.filled_windows} · 剩余缺报 ${report.remaining_missing_windows} · 同窗同数去重 ${report.duplicate_supplements} 份 · 计数冲突 ${report.conflict_windows} 个</p>`;
  const conflictTable = conflicts.length ? `<h4>计数冲突（按缺报处理，未纳入）</h4>${table(conflicts.map(row => ({作业区:row.zone,配对号:row.pair,班次号:row.block,窗口起点:row.window_start,冲突计数:row.counts.map(item => `${item.on_time}/${item.eligible}`).join('、'),无效补报:row.invalid_supplements})))}` : '';
  const unresolvedTable = unresolved.length ? `<h4>分配顺序未定的配对（补报顺序不一致，未纳入）</h4>${table(unresolved.map(row => ({作业区:row.zone,配对号:row.pair})))}` : '';
  return `<div class="notice">${summary}${conflictTable}${unresolvedTable}<p class="hint">同窗同数按一份计；计数冲突的窗口仍按缺报，需核对源日志后重传。</p></div>`;
}

export async function source() {
  const profile = state.profile;
  const revisions = await api('/datasets');
  const active = revisions.find(row => row.id === state.datasetId) || revisions[0];
  const detail = active ? await api(`/datasets/${active.id}`) : null;
  const importer = state.account.role === 'analyst' ? `<form id="upload" class="inline-form"><label>导入数据文件<input name="file" type="file" accept=".csv,text/csv" required></label><button type="submit">保存数据版本</button>${errorSlot}</form>` : '';
  const bases = revisions.filter(row => row.kind !== 'supplement');
  const baseId = detail?.base_id ?? bases[0]?.id;
  const supplementer = state.account.role === 'analyst' && bases.length ? `<form id="supplement" class="inline-form"><label>补报基底<select name="base_id">${bases.map(row => `<option value="${row.id}" ${row.id === baseId ? 'selected' : ''}>${esc(versionLabel(row))}</option>`).join('')}</select></label><label>补报文件<input name="file" type="file" accept=".csv,text/csv" required></label><button type="submit">提交补报</button>${errorSlot}</form><p class="hint">补报只能填补冻结排班里最初缺报的窗口；同窗同数按一份计，计数冲突仍按缺报。返回核对时，窗口与结论一起切换；补报未通过时保留原生效版本。</p>` : '';
  return `<section><p>${esc(profile.input)}</p><p class="hint">CSV 表头顺序可调整，列名需匹配样例。</p>${importer}${supplementer}<button type="button" id="example" class="secondary">下载样例 CSV</button><p id="download-error" role="alert" class="error"></p></section>${detail ? `<section><label>数据版本<select id="revision">${revisions.map(row => `<option value="${row.id}" ${row.id === active.id ? 'selected' : ''}>${esc(versionLabel(row))}</option>`).join('')}</select></label><p class="hint">${esc(kinds[detail.kind] ?? '数据版本')} · 基底 #${detail.base_id} · 版本 v${detail.version} · ${detail.count} 行 · 页面预览前 ${detail.records.length} 行 · SHA-256 ${esc(detail.checksum.slice(0,12))}${detail.supplements?.length ? ` · 已收补报 ${detail.supplements.length} 份` : ''}</p>${mergeBox(detail.merge_report?.report)}${table(detail.records,profile.columns)}</section>` : empty('请导入项目样例或业务 CSV。')}`;
}

export function bindSource(root, refresh) {
  bindForm(root, '#upload', async form => {
    const file = form.elements.file.files[0];
    const imported = await api('/datasets', {name:file.name,csv:await file.text()});
    state.datasetId = imported.id;
    state.status = imported.created ? `已保存 ${imported.rows} 行。` : '数据与已有版本一致，已选中该版本。';
  }, refresh);
  bindForm(root, '#supplement', async form => {
    const file = form.elements.file.files[0];
    const result = await api('/datasets/supplement', {base_id:Number(form.elements.base_id.value),name:file.name,csv:await file.text()});
    state.datasetId = result.dataset_id;
    state.status = result.changed
      ? `补报已合并：填补 ${result.report.filled_windows} 个窗口，剩余缺报 ${result.report.remaining_missing_windows} 个，版本 v${result.version} 已生效。`
      : `补报与已生效版本一致，未新增窗口（剩余缺报 ${result.report.remaining_missing_windows} 个）。`;
  }, refresh);
  root.querySelector('#revision')?.addEventListener('change', event => {
    state.datasetId = Number(event.target.value);
    refresh();
  });
  root.querySelector('#example')?.addEventListener('click', async () => {
    try {await download('/example.csv', state.profile.filename);}
    catch (error) {root.querySelector('#download-error').textContent = error.message;}
  });
}
