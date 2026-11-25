import {api,download} from '../api/client.js';
import {empty,errorSlot} from '../components/common/layout.js';
import {table} from '../components/common/table.js';
import {sources} from '../constants/enums.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {fieldInput} from '../types/fields.js';
import {esc,formData,time} from '../utils/html.js';

const statusChip = item => `<span class="chip chip-${esc(item.source)}">${esc(sources[item.source] || item.source)}</span>`;

function assignmentRows(result) {
  return result.assignments.map(item => ({
    设备:item.device,
    分区:item.zone,
    班组:item.crew,
    安排时段:`${item.window}（${item.duration} 分钟）`,
    状态:statusChip(item),
    _source:item.source,
    _device:item.device,
  }));
}

function crewRows(result) {
  return result.crews.map(row => ({
    班组:row.crew,
    能到的分区:row.zones.join('、') || '—',
    可用时段:row.windows.join('；') || '—',
    已接单:row.claimed,
    已发布:row.published,
    建议:row.proposed,
    占用工时:`${row.minutes} 分钟`,
  }));
}

function unassignedRows(result) {
  return result.unassigned.map(item => ({设备:item.device,分区:item.zone,当前状态:item.status,未排原因:item.reason}));
}

export async function remediation() {
  const profile = state.profile;
  const [datasets,plans] = await Promise.all([api('/datasets'),api('/remediation')]);
  const planId = state.remediationId ?? plans.plans[0]?.id ?? null;
  const detail = planId ? await api(`/remediation/${planId}`) : null;
  const operator = state.account.role !== 'viewer';
  const form = operator && datasets.length ? `<form id="plan" class="inline-form"><label>数据版本<select name="dataset_id">${datasets.map(row => `<option value="${row.id}" ${row.id === state.datasetId ? 'selected' : ''}>${esc(row.name)} · #${row.id}</option>`).join('')}</select></label>${profile.remediation_options.map(field => `<label>${esc(field.label)}${fieldInput(field,esc)}</label>`).join('')}<button type="submit">生成补检排班</button>${errorSlot}</form>` : empty(datasets.length ? '复核员或分析员可生成排班。' : '先导入数据版本。','#1','查看数据台账');
  const picker = plans.plans.length > 1 ? `<label>补检排班<select id="pick-plan">${plans.plans.map(row => `<option value="${row.id}" ${row.id === planId ? 'selected' : ''}>#${row.id} · ${esc(row.shift_date)} · 版本 ${row.revision}${row.headline ? ' · ' + esc(row.headline) : ''}</option>`).join('')}</select></label>` : '';
  const body = detail ? planBody(detail,operator) : empty('先生成一份三区补检排班。');
  return `<section><h2>补检排班条件</h2><p>汇总同班三区缺心跳或过期设备的补检安排：按班组能到的分区、可用时段和设备整段工时排入，已接单与已发布的安排保持不变，其余重算争取本班排上最多台数。</p>${form}${picker}</section>${body}`;
}

function planBody(result,operator) {
  const exportButton = `<button id="plan-download" class="secondary" data-id="${result.id}" type="button">下载排班 CSV</button>`;
  const actions = operator ? `<form id="plan-actions" class="inline-form" data-id="${result.id}" data-revision="${result.revision}"><button type="submit" name="action" value="recompute" class="secondary">重算余下安排</button><button type="submit" name="action" value="publish">发布建议派单</button>${errorSlot}</form>` : '';
  const rows = assignmentRows(result);
  const assignTable = rows.length ? `<div class="table-wrap" tabindex="0"><table><thead><tr><th>设备</th><th>分区</th><th>班组</th><th>安排时段</th><th>状态</th>${operator ? '<th>操作</th>' : ''}</tr></thead><tbody>${rows.map(row => `<tr><td>${esc(row.设备)}</td><td>${esc(row.分区)}</td><td>${esc(row.班组)}</td><td>${esc(row.安排时段)}</td><td>${row.状态}</td>${operator ? `<td>${actionButtons(result.id,row)}</td>` : ''}</tr>`).join('')}</tbody></table></div>` : '<p class="empty">本班没有需要补检的设备，或全部设备都未排上。请查看下方原因。</p>';
  return `<section class="reading"><h2>${esc(result.headline)}</h2><dl class="metrics"><div><dt>需补检台数</dt><dd>${result.metrics.needed}</dd></div><div><dt>已排台数</dt><dd>${result.metrics.covered}</dd></div><div><dt>已接单</dt><dd>${result.metrics.claimed}</dd></div><div><dt>已发布</dt><dd>${result.metrics.published}</dd></div><div><dt>建议新排</dt><dd>${result.metrics.planned}</dd></div><div><dt>未排上</dt><dd>${result.metrics.unassigned}</dd></div><div><dt>版本</dt><dd>${result.revision}</dd></div></dl><p class="hint">晨检 ${esc(result.shift_date)} · 截点 ${esc(result.cutoff)} · 新鲜度 ${result.freshness_minutes} 分钟${result.published_at ? ` · 最近发布 ${time(result.published_at)}` : ''}</p>${exportButton}${actions}<p id="plan-error" class="error" role="alert"></p><h3>班组与可用时段</h3>${table(crewRows(result))}<h3>排班明细</h3>${assignTable}<h3>未排上的设备与原因</h3>${table(unassignedRows(result))}<h3>口径与限制</h3><ul>${result.notes.map(note => `<li>${esc(note)}</li>`).join('')}</ul></section>`;
}

function actionButtons(planId,row) {
  if (row._source === 'planned') {
    return `<button type="button" class="secondary claim" data-id="${planId}" data-device="${esc(row._device)}">接单</button>`;
  }
  if (row._source === 'claimed') {
    return `<button type="button" class="secondary release" data-id="${planId}" data-device="${esc(row._device)}">撤销接单</button>`;
  }
  return '<span class="hint">已发布</span>';
}

export function bindRemediation(root,refresh) {
  bindForm(root,'#plan',async form => {
    const {dataset_id,...options} = formData(form);
    const created = await api('/remediation',{dataset_id:Number(dataset_id),options});
    state.remediationId = created.id;
    state.status = '补检排班已生成。';
  },refresh);
  root.querySelector('#pick-plan')?.addEventListener('change', event => {
    state.remediationId = Number(event.target.value);
    refresh();
  });
  bindForm(root,'#plan-actions',async (form,button) => {
    const id = form.dataset.id;
    if (button.value === 'recompute') {
      await api(`/remediation/${id}/recompute`,{});
      state.status = '已按已接单的安排重算余下设备。';
    } else {
      const published = await api(`/remediation/${id}/publish`,{revision:Number(form.dataset.revision)});
      state.status = published.created ? `已发布 ${published.created} 条建议派单。` : '没有新的建议派单可发布。';
    }
  },refresh);
  const report = message => {
    const slot = root.querySelector('#plan-error');
    if (slot) slot.textContent = message;
  };
  root.querySelectorAll('button.claim').forEach(button => button.addEventListener('click',async event => {
    const target = event.currentTarget;
    try {
      await api(`/remediation/${target.dataset.id}/claims`,{device:target.dataset.device});
      state.status = `已接单：${target.dataset.device}。`;
      await refresh();
    } catch (error) {
      state.status = '';
      report(error.message);
    }
  }));
  root.querySelectorAll('button.release').forEach(button => button.addEventListener('click',async event => {
    const target = event.currentTarget;
    try {
      await api(`/remediation/${target.dataset.id}/claims/${encodeURIComponent(target.dataset.device)}`,null,'DELETE');
      state.status = `已撤销接单：${target.dataset.device}。`;
      await refresh();
    } catch (error) {
      state.status = '';
      report(error.message);
    }
  }));
  root.querySelector('#plan-download')?.addEventListener('click',async event => {
    const id = event.currentTarget.dataset.id;
    try {await download(`/remediation/${id}/report.csv`,`补检排班-${id}.csv`);}
    catch (error) {report(error.message);}
  });
}
