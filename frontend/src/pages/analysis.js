import {api} from '../api/client.js';
import {empty,errorSlot} from '../components/common/layout.js';
import {report} from '../components/common/report.js';
import {table} from '../components/common/table.js';
import {decisions,states} from '../constants/enums.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {fieldInput} from '../types/fields.js';
import {esc,formData,time} from '../utils/html.js';

function runBadge(run) {
  if (run.is_current) return '当前生效';
  if (run.superseded) return '已被新版取代';
  if (run.pending) return '待生效';
  return decisions[run.decision] ?? '待复核';
}

export async function analysis() {
  const profile = state.profile;
  const [datasets,runs] = await Promise.all([api('/datasets'),api('/runs')]);
  const usable = datasets.filter(row => row.kind !== 'supplement');
  const preferred = usable.find(row => row.id === state.datasetId) || usable.find(row => row.is_active_version) || usable[0];
  const form = state.account.role === 'analyst' && usable.length ? `<form id="queue" class="inline-form"><label>数据版本<select name="dataset_id">${usable.map(row => `<option value="${row.id}" ${row.id === preferred?.id ? 'selected' : ''}>${esc(row.name)} · #${row.id}${row.version ? ` · v${row.version}` : ''}${row.is_active_version ? ' · 当前生效' : ''}</option>`).join('')}</select></label>${profile.options.map(field => `<label>${esc(field.label)}${fieldInput(field,esc)}</label>`).join('')}<button type="submit">提交计算</button>${errorSlot}</form>` : empty(usable.length ? '分析员可提交计算。' : '先导入数据版本。', '#1', '查看数据台账');
  return `<section><h2>计算条件</h2><p>${esc(profile.method)}</p>${form}</section><section><h2>分析记录</h2>${table(runs.map(run => ({任务:{link:`#3/${run.id}`,text:`#${run.id}`},数据版本:`#${run.dataset_id}${run.version ? ` · v${run.version}` : ''}`,状态:states[run.state],版本位置:runBadge(run),提交时间:time(run.created),复核:decisions[run.decision] ?? '待复核'})))}</section>`;
}

export async function currentRun() {
  const runs = await api('/runs');
  const chosen = runs.find(item => item.id === state.runId) || runs.find(item => item.is_current) || runs[0];
  return {runs,run:chosen ? await api(`/runs/${chosen.id}`) : null};
}

export function chooseRun(runs, selected) {
  return `<label>分析任务<select id="pick-run">${runs.map(row => `<option value="${row.id}" ${selected === row.id ? 'selected' : ''}>#${row.id} · ${states[row.state]} · ${runBadge(row)} · ${time(row.created)}</option>`).join('')}</select></label>`;
}

function versionNotice(run) {
  if (!run.result) return '';
  if (run.superseded) return `<p class="notice">该结果基于已被新版取代的数据版本 v${run.version}。补报已产生更新的窗口与结论，请查看当前生效版本；已批准的报告仍按其当时的窗口复算。</p>`;
  if (run.pending) return `<p class="notice">该结果基于较新的数据版本 v${run.version}，尚未生效。页面继续显示当前生效版本 v${run.active_version} 的窗口与结论，待新版计算完成后再一起切换。</p>`;
  return '';
}

export async function results() {
  const {runs,run} = await currentRun();
  if (!run) return empty('提交一条分析任务后可在这里查看结果。','#2','配置分析');
  const result = run.result ? report(run.result) : `<p ${run.error ? 'class="error"' : 'data-poll'}>${esc(run.error || states[run.state])}</p>`;
  const approval = run.approved_version != null ? `<p class="hint">已批准版本 v${run.approved_version}（数据版本 #${run.approved_dataset_id}）。导出与投递均按该版本窗口复算。</p>` : '';
  return `<section>${chooseRun(runs,run.id)}<p class="hint">数据版本 #${run.dataset_id}${run.version ? ` · 版本 v${run.version}` : ''} · 计算状态 ${states[run.state]}${run.is_current ? ' · 当前生效' : run.superseded ? ' · 已被新版取代' : run.pending ? ' · 待生效' : ''}</p>${versionNotice(run)}${approval}${result}${run.result ? `<a class="button" href="#4/${run.id}">进入业务复核</a>` : '<a href="#2">回到分析条件</a>'}</section>`;
}

export function bindAnalysis(root, section, refresh) {
  bindForm(root, '#queue', async form => {
    const {dataset_id,...options} = formData(form);
    const run = await api('/runs',{dataset_id:Number(dataset_id),options});
    location.hash = `3/${run.id}`;
  });
  root.querySelector('#pick-run')?.addEventListener('change', event => {location.hash = `${section}/${event.target.value}`;});
}
