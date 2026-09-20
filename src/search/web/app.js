/* 就业信息检索前端：原生 JS，零构建、零 CDN、离线可用。
 *
 * 安全约定（重要）：结果标题/片段/字段值都来自**抓取的第三方网页文本**，
 * 因此一律先转义再渲染。片段里的高亮标记由后端生成，前端在渲染前会先剥掉
 * 所有标签再按检索词重新包 <em>，避免"直接信任后端 HTML"这条链路被滥用。
 */

const CORE_FIELDS = [
  ['graduation_year', '届别'],
  ['grade', '年级'],
  ['degree', '学历'],
  ['major', '专业'],
  ['city', '城市'],
  ['employer', '单位'],
  ['position', '岗位'],
];

const PAGE_SIZE = 20;

const state = {
  q: '',
  fields: {},
  offset: 0,
  total: 0,
  job: null,
  pollTimer: null,
};

/* ---------- 工具 ---------- */

const $ = (id) => document.getElementById(id);

function escapeHtml(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** 剥掉全部标签，得到纯文本（用于重新构造安全的高亮片段）。 */
function stripTags(html) {
  const holder = document.createElement('div');
  holder.innerHTML = String(html || '');
  return holder.textContent || '';
}

/** 在转义后的纯文本上，按检索词包 <em> 高亮。 */
function highlight(rawText, keywords) {
  let safe = escapeHtml(rawText);
  const terms = (keywords || []).filter((t) => t && t.length);
  // 长词优先，避免短词先匹配把长词切碎
  terms.sort((a, b) => b.length - a.length);
  terms.forEach((term) => {
    const escapedTerm = escapeHtml(term);
    if (!escapedTerm) return;
    safe = safe.split(escapedTerm).join(`<em>${escapedTerm}</em>`);
  });
  return safe;
}

function keywordsOf(query) {
  return String(query || '').trim().split(/[\s,，、;；/|]+/).filter(Boolean);
}

function setNotice(message, isError) {
  const node = $('notice');
  if (!message) {
    node.hidden = true;
    node.textContent = '';
    return;
  }
  node.hidden = false;
  node.textContent = message;
  node.classList.toggle('error', Boolean(isError));
}

/* ---------- 检索 ---------- */

function buildParams(offset) {
  const params = new URLSearchParams();
  if (state.q) params.set('q', state.q);
  params.set('limit', String(PAGE_SIZE));
  params.set('offset', String(offset || 0));
  Object.entries(state.fields).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  return params;
}

async function runSearch(offset) {
  const target = offset || 0;
  const url = `/api/search?${buildParams(target).toString()}`;
  const submit = $('submit-btn');
  submit.disabled = true;
  setNotice('');

  try {
    const response = await fetch(url);
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.error || `HTTP ${response.status}`);
    }
    const data = await response.json();
    state.offset = target;
    state.total = data.total || 0;
    syncUrl();
    renderMeta(data);
    renderResults(data);
    renderRelated(data.related);
    renderSuggestion(data);
    renderPager(data);
    loadStats();
  } catch (error) {
    setNotice(`检索失败：${error.message}`, true);
  } finally {
    submit.disabled = false;
  }
}

function syncUrl() {
  const params = buildParams(state.offset);
  params.delete('limit');
  params.delete('offset');
  const query = params.toString();
  const suffix = state.offset ? `&offset=${state.offset}` : '';
  history.replaceState(null, '', query ? `/?${query}${suffix}` : '/');
}

/* ---------- 渲染 ---------- */

function renderMeta(data) {
  const meta = $('result-meta');
  if (!state.q && Object.keys(state.fields).length === 0) {
    meta.hidden = true;
    return;
  }
  meta.hidden = false;
  const cacheLabel = { l1: '进程内缓存', l2: '查询缓存', l3: '库内检索' }[data.source] || data.source;
  meta.innerHTML = `
    <span>找到 <b>${Number(data.total || 0)}</b> 条${state.q ? `与「${escapeHtml(state.q)}」` : ''}相关的记录</span>
    <span class="badge cache-${escapeHtml(data.source)}">${escapeHtml(cacheLabel)} · ${escapeHtml(String(data.elapsed_ms))} ms</span>
    <span class="badge">数据版本 v${Number(data.index_version || 0)}</span>`;
}

function renderResults(data) {
  const list = $('results');
  const empty = $('empty');
  const hits = data.hits || [];
  list.innerHTML = '';
  empty.hidden = hits.length > 0;
  if (!hits.length) return;

  const keywords = keywordsOf(state.q);
  hits.forEach((hit) => {
    const item = document.createElement('li');
    item.className = 'result';

    const title = document.createElement('a');
    title.className = 'result-title';
    title.textContent = hit.title || '(无标题)';
    title.href = hit.source_url || '#';
    title.target = '_blank';
    title.rel = 'noopener noreferrer';
    item.appendChild(title);

    const fields = document.createElement('div');
    fields.className = 'result-fields';
    CORE_FIELDS.forEach(([key, label]) => {
      const value = (hit.fields || {})[key] || '未知';
      const tag = document.createElement('span');
      const missing = value === '未知';
      tag.className = `field-tag${missing ? ' missing' : ''}`;
      tag.innerHTML = `${escapeHtml(label)}：<b>${escapeHtml(missing ? '待补充' : value)}</b>`;
      fields.appendChild(tag);
    });
    item.appendChild(fields);

    const snippet = document.createElement('p');
    snippet.className = 'result-snippet';
    snippet.innerHTML = highlight(stripTags(hit.snippet), keywords);
    item.appendChild(snippet);

    const foot = document.createElement('div');
    foot.className = 'result-foot';
    (hit.highlights || []).forEach((label) => {
      const tag = document.createElement('span');
      tag.className = 'hit-tag';
      tag.textContent = `命中 ${label}`;
      foot.appendChild(tag);
    });
    const count = Number(hit.complete_field_count || 0);
    const completeness = document.createElement('span');
    completeness.className = count === 7 ? 'completeness full' : '';
    completeness.textContent = `字段完整度 ${count}/7`;
    foot.appendChild(completeness);
    if (hit.publish_date) {
      const date = document.createElement('span');
      date.textContent = hit.publish_date;
      foot.appendChild(date);
    }
    const source = document.createElement('a');
    source.href = hit.source_url || '#';
    source.target = '_blank';
    source.rel = 'noopener noreferrer';
    source.textContent = '回原文核对';
    foot.appendChild(source);
    item.appendChild(foot);

    list.appendChild(item);
  });
}

function renderRelated(related) {
  const card = $('related-card');
  const box = $('related-list');
  box.innerHTML = '';
  const items = related || [];
  card.hidden = items.length === 0;
  items.forEach((term) => {
    const button = document.createElement('button');
    button.className = 'related';
    button.type = 'button';
    button.textContent = term;
    button.addEventListener('click', () => {
      // 关联搜索：把词并进当前查询，而不是直接替换，保留用户的原始意图
      const current = keywordsOf(state.q);
      if (!current.includes(term)) current.push(term);
      $('q').value = current.join(' ');
      state.offset = 0;
      runSearch(0);
    });
    box.appendChild(button);
  });
}

function renderSuggestion(data) {
  const box = $('suggestion');
  const suggestion = data.suggestion;
  box.innerHTML = '';
  if (!suggestion) {
    box.hidden = true;
    return;
  }
  box.hidden = false;

  const heading = document.createElement('h3');
  heading.textContent = suggestion.reason || '库内记录不足';
  box.appendChild(heading);

  const cost = document.createElement('div');
  cost.className = 'cost';
  cost.textContent =
    `定向采集预估：${suggestion.estimated_requests} 次请求 / 约 ${suggestion.estimated_seconds} 秒` +
    `（合规限速 ≥2 秒/请求，不可调低）`;
  box.appendChild(cost);

  const actions = document.createElement('div');
  actions.className = 'actions';

  if (suggestion.allowed) {
    const button = document.createElement('button');
    button.className = 'btn-primary';
    button.type = 'button';
    button.textContent = '去门户采集相关通知';
    button.addEventListener('click', () => triggerFetch(button));
    actions.appendChild(button);
    const note = document.createElement('span');
    note.className = 'cost';
    note.textContent = '采集中页面可继续使用；完成后会自动刷新结果';
    actions.appendChild(note);
  } else {
    const note = document.createElement('span');
    note.className = 'cost';
    note.innerHTML = `当前不可采集：${escapeHtml(suggestion.blocked_reason || '未开启')}`;
    actions.appendChild(note);
  }
  box.appendChild(actions);
}

function renderPager(data) {
  const pager = $('pager');
  const total = Number(data.total || 0);
  pager.hidden = total <= PAGE_SIZE;
  if (pager.hidden) return;
  const start = state.offset + 1;
  const end = Math.min(state.offset + (data.hits || []).length, total);
  $('page-info').textContent = `${start} - ${end} / 共 ${total} 条`;
  $('prev-page').disabled = state.offset <= 0;
  $('next-page').disabled = state.offset + PAGE_SIZE >= total;
}

/* ---------- 统计 ---------- */

async function loadStats() {
  try {
    const response = await fetch('/api/stats');
    if (!response.ok) return;
    const stats = await response.json();
    const rows = [
      ['已索引记录', stats.indexed_rows],
      ['数据版本号', `v${stats.index_version}`],
      ['L1 命中 / 未命中', `${stats.l1_hits} / ${stats.l1_misses}`],
      ['L2 缓存查询数', stats.query_cache_rows],
      ['采集开关', stats.trigger_enabled ? '已开启' : '关闭'],
    ];
    $('stats').innerHTML = rows
      .map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(String(value))}</dd></div>`)
      .join('');
    $('trigger-note').textContent = stats.trigger_enabled
      ? '采集开关已开启：库内不足时可发起定向采集（受冷却与预算约束）。'
      : '采集开关关闭（search.trigger_enabled=false）：检索完全离线，不会发出任何网络请求。';
  } catch (error) {
    /* 统计失败不影响检索 */
  }
}

/* ---------- 定向采集 ---------- */

async function triggerFetch(button) {
  button.disabled = true;
  button.textContent = '正在采集…';
  setNotice('');
  try {
    const response = await fetch('/api/fetch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ q: state.q, limit: PAGE_SIZE }),
    });
    const data = await response.json();
    const job = data.job || {};
    if (!job.job_id) {
      setNotice(`未发起采集：${job.message || '被拒绝'}`, true);
      button.disabled = false;
      button.textContent = '去门户采集相关通知';
      return;
    }
    state.job = job;
    pollJob(job.job_id);
  } catch (error) {
    setNotice(`采集请求失败：${error.message}`, true);
    button.disabled = false;
    button.textContent = '去门户采集相关通知';
  }
}

function pollJob(jobId) {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    try {
      const response = await fetch(`/api/fetch/${encodeURIComponent(jobId)}`);
      const data = await response.json();
      const job = data.job;
      if (!job) return;
      if (job.state === 'running' || job.state === 'pending') {
        setNotice(`正在定向采集…（已取 ${job.details_ok || 0} 条）`);
        return;
      }
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      if (job.state === 'done') {
        setNotice(job.message || '采集完成，正在刷新结果…');
        runSearch(0);
      } else {
        setNotice(`采集${job.state === 'skipped' ? '被跳过' : '失败'}：${job.message || ''}`, true);
      }
      loadStats();
    } catch (error) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      setNotice(`查询采集状态失败：${error.message}`, true);
    }
  }, 2000);
}

/* ---------- 事件绑定与初始化 ---------- */

function currentFields() {
  const form = $('filter-form');
  const fields = {};
  CORE_FIELDS.forEach(([key]) => {
    const value = (form.elements[key] ? form.elements[key].value : '').trim();
    if (value) fields[key] = value;
  });
  return fields;
}

function init() {
  const params = new URLSearchParams(location.search);
  const initial = params.get('q') || '';
  if (initial) $('q').value = initial;
  state.q = initial;
  state.fields = {};
  CORE_FIELDS.forEach(([key]) => {
    const value = params.get(key);
    if (value) {
      state.fields[key] = value;
      const input = $('filter-form').elements[key];
      if (input) input.value = value;
    }
  });

  const hero = $('hero');
  const filters = $('filters');
  const hasQuery = Boolean(initial) || Object.keys(state.fields).length > 0;
  const applyState = (active) => {
    hero.classList.toggle('compact', active);
    filters.hidden = !active;
  };
  applyState(hasQuery);

  $('search-form').addEventListener('submit', (event) => {
    event.preventDefault();
    state.q = $('q').value.trim();
    state.fields = currentFields();
    state.offset = 0;
    applyState(Boolean(state.q) || Object.keys(state.fields).length > 0);
    if (!state.q && Object.keys(state.fields).length === 0) {
      setNotice('请输入关键词，或填写至少一个字段过滤条件');
      return;
    }
    runSearch(0);
  });

  $('filter-form').addEventListener('submit', (event) => {
    event.preventDefault();
    state.fields = currentFields();
    state.q = $('q').value.trim();
    state.offset = 0;
    runSearch(0);
  });

  $('clear-filters').addEventListener('click', () => {
    $('filter-form').reset();
    state.fields = {};
    runSearch(0);
  });

  document.querySelectorAll('.chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      $('q').value = chip.dataset.q || '';
      $('search-form').dispatchEvent(new Event('submit', { cancelable: true }));
    });
  });

  $('prev-page').addEventListener('click', () => runSearch(Math.max(0, state.offset - PAGE_SIZE)));
  $('next-page').addEventListener('click', () => runSearch(state.offset + PAGE_SIZE));

  if (hasQuery) runSearch(0);
  else loadStats();

  // 刷新页面后恢复正在进行的采集进度
  fetch('/api/fetch')
    .then((response) => (response.ok ? response.json() : { job: null }))
    .then((data) => {
      const job = data.job;
      if (job && (job.state === 'running' || job.state === 'pending')) {
        state.job = job;
        state.q = state.q || job.search_value || '';
        pollJob(job.job_id);
      }
    })
    .catch(() => {});
}

document.addEventListener('DOMContentLoaded', init);
