'use strict';
const $ = (q, root = document) => root.querySelector(q);
const $$ = (q, root = document) => [...root.querySelectorAll(q)];
const icon = (name) =>
  `<svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><use href="#i-${name}"/></svg>`;
const esc = (value) =>
  String(value ?? '').replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
const state = {
  documents: [],
  collections: [],
  engine: {},
  session: '',
  view: 'library',
  collection: '',
  query: '',
  matches: null,
  filter: 'all',
  sort: 'newest',
  layout: 'grid',
  selecting: false,
  selected: new Set(),
  reader: null,
  editing: false,
  saving: false,
  zoom: 1,
  rotation: 0,
  page: 1,
  importCollection: '未分诗集',
  importing: false,
  polling: false,
  listPage: 1,
};
const PAGE_SIZE = 48;
let searchTimer,
  searchRequest = 0,
  refreshRequest = 0,
  readerRequest = 0;
let toastTimer;
function toast(message) {
  $('#toast').textContent = message;
  $('#toast').classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('#toast').classList.remove('visible'), 4500);
}
async function api(url, options = {}) {
  const headers = { 'X-Shijian-Session': state.session, ...options.headers };
  if (options.body && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(url, { ...options, headers });
  if (!response.ok) {
    let error;
    try {
      error = (await response.json()).detail;
    } catch {
      error = '请求失败';
    }
    throw Error(typeof error === 'string' ? error : '输入格式有误，请检查后再试。');
  }
  return response.json();
}
async function refresh() {
  const request = ++refreshRequest;
  const data = await api('/api/state');
  if (request !== refreshRequest) return;
  const current = new Map(state.documents.map((d) => [d.id, d]));
  data.documents = data.documents.map((d) =>
    current.get(d.id)?.updated_at > d.updated_at ? current.get(d.id) : d,
  );
  Object.assign(state, {
    documents: data.documents,
    collections: data.collections,
    engine: data.engine,
    session: data.session,
  });
  const ids = new Set(state.documents.map((d) => d.id));
  state.selected = new Set([...state.selected].filter((id) => ids.has(id)));
  renderNav();
  await refreshSearch();
  if (!state.reader) renderLibrary();
  await refreshReader();
}

async function refreshSearch() {
  const query = state.query,
    request = ++searchRequest;
  if (!query) {
    state.matches = null;
    return;
  }
  const result = await api('/api/search?q=' + encodeURIComponent(query));
  if (request === searchRequest && query === state.query) state.matches = new Set(result.ids);
}

function searchDocuments(query) {
  clearTimeout(searchTimer);
  searchRequest++;
  state.query = query.trim();
  state.matches = null;
  state.listPage = 1;
  renderLibrary();
  searchTimer = setTimeout(async () => {
    try {
      await refreshSearch();
      if (!state.reader) renderLibrary();
    } catch (e) {
      toast(e.message);
    }
  }, 250);
}

async function refreshReader() {
  const reader = state.reader;
  if (!reader || state.editing) return;
  const summary = state.documents.find((d) => d.id === reader.id);
  if (!summary || summary.updated_at <= reader.updated_at) return;
  const latest = await api('/api/documents/' + encodeURIComponent(reader.id));
  // An edit, navigation, or another detail response may have happened while awaiting.
  if (state.reader !== reader || state.editing) return;
  state.reader = latest;
  renderReader();
}

async function pollTasks() {
  const ids = state.documents
    .filter((d) => ['running', 'queued'].includes(d.status))
    .map((d) => d.id);
  if (state.importing || state.polling) return;
  state.polling = true;
  try {
    if (!ids.length) {
      await refreshReader();
      return;
    }
    const data = await api('/api/tasks/status', { method: 'POST', body: { ids } });
    const updates = new Map(data.documents.map((d) => [d.id, d]));
    let changed = false;
    state.documents = state.documents.map((d) => {
      const update = updates.get(d.id);
      if (!update || update.updated_at < d.updated_at) return d;
      if (update.updated_at !== d.updated_at || update.status !== d.status) {
        changed = true;
        return update;
      }
      return d;
    });
    state.engine = data.engine;
    if (changed) {
      renderNav();
      await refreshSearch();
      if (!state.reader) renderLibrary();
    }
    // Retry detail refresh even if the previous attempt failed after the task finished.
    await refreshReader();
  } finally {
    state.polling = false;
  }
}
function visibleDocs() {
  let docs = state.documents.filter((d) => (state.view === 'trash' ? d.trashed : !d.trashed));
  if (state.view === 'demo') docs = docs.filter((d) => d.demo);
  else if (state.view !== 'trash') docs = docs.filter((d) => !d.demo);
  if (state.view === 'favorites') docs = docs.filter((d) => d.favorite);
  if (state.view === 'review') docs = docs.filter((d) => d.status === 'done' && !d.reviewed);
  if (state.view === 'queue')
    docs = docs.filter((d) =>
      ['new', 'queued', 'running', 'failed', 'interrupted'].includes(d.status),
    );
  if (state.collection) docs = docs.filter((d) => d.collection === state.collection);
  if (state.query) docs = docs.filter((d) => state.matches?.has(d.id));
  if (state.filter === 'new') docs = docs.filter((d) => d.status === 'new');
  if (state.filter === 'review') docs = docs.filter((d) => d.status === 'done' && !d.reviewed);
  if (state.filter === 'reviewed') docs = docs.filter((d) => d.reviewed);
  if (state.filter === 'failed')
    docs = docs.filter((d) => ['failed', 'interrupted'].includes(d.status));
  docs.sort(
    state.sort === 'title'
      ? (a, b) => a.title.localeCompare(b.title, 'zh-Hans-CN')
      : (a, b) => (state.sort === 'oldest' ? 1 : -1) * a.created_at.localeCompare(b.created_at),
  );
  return docs;
}
const views = {
  library: '全部诗稿',
  favorites: '我的收藏',
  review: '待校对',
  queue: '识别任务',
  demo: '示例诗馆',
  trash: '回收站',
};
function renderNav() {
  const docs = state.documents.filter((d) => !d.demo && !d.trashed);
  $('#count-library').textContent = docs.length;
  $('#count-favorites').textContent = docs.filter((d) => d.favorite).length;
  $('#count-review').textContent = docs.filter((d) => d.status === 'done' && !d.reviewed).length;
  $('#count-queue').textContent = docs.filter((d) =>
    ['new', 'running', 'queued', 'failed', 'interrupted'].includes(d.status),
  ).length;
  $$('.nav-item[data-view]').forEach((b) =>
    b.classList.toggle('active', b.dataset.view === state.view && !state.collection),
  );
  $('#collections').innerHTML = state.collections
    .map(
      (name) =>
        `<button class="nav-item ${state.collection === name ? 'active' : ''}" data-collection="${esc(name)}"><span class="collection-dot"></span>${esc(name)}<span>${(state.view === 'demo' ? state.documents.filter((d) => d.demo && !d.trashed) : docs).filter((d) => d.collection === name).length}</span></button>`,
    )
    .join('');
  $$('[data-collection]').forEach(
    (b) =>
      (b.onclick = () =>
        navigate(state.view === 'demo' ? 'demo' : 'library', b.dataset.collection)),
  );
  $('#breadcrumb-current').textContent = state.collection || views[state.view];
}
function badge(doc) {
  const status =
    doc.reviewed && doc.status === 'done'
      ? 'reviewed'
      : doc.status === 'done'
        ? 'review'
        : doc.status;
  const label =
    {
      reviewed: '已校对',
      review: '待校对',
      new: '待识别',
      queued: '排队中',
      running: '识别中',
      failed: '识别失败',
      interrupted: '已中断',
    }[status] || status;
  return `<span class="badge ${esc(status)}">${status === 'reviewed' ? icon('check') : status === 'running' ? '<span class="busy-spinner"></span>' : ''}${label}</span>`;
}
function renderLibrary() {
  const all = state.documents.filter((d) => !d.demo && !d.trashed),
    docs = visibleDocs();
  const pages = Math.max(1, Math.ceil(docs.length / PAGE_SIZE));
  state.listPage = Math.min(state.listPage, pages);
  const pageDocs = docs.slice((state.listPage - 1) * PAGE_SIZE, state.listPage * PAGE_SIZE);
  const special = (state.view !== 'library' && state.view !== 'demo') || !!state.collection;
  $('#hero').hidden = special || (all.length > 0 && state.view !== 'demo');
  $('#page-title').textContent =
    state.collection || (special ? views[state.view] : '把诗意，留在时光里。');
  $('#page-subtitle').textContent =
    state.view === 'queue'
      ? '按顺序在本机识别。完成后，请与原稿逐字校对。'
      : state.view === 'review'
        ? '原稿在左，文字在右。把每一个字，认真留下。'
        : state.view === 'trash'
          ? '移入这里的诗稿仍保留原图和文字，可以随时恢复。'
          : '拾起散落的手稿，让每一句诗，都有归处。';
  $('#section-title').textContent =
    state.collection ||
    (state.view === 'demo'
      ? '示例诗馆'
      : state.view === 'library'
        ? '我的诗稿'
        : views[state.view]);
  $('#section-count').textContent = `共 ${docs.length} 份`;
  $('#demo-notice').hidden = state.view !== 'demo';
  $('#stats').hidden = special;
  $('#stats').innerHTML = [
    ['book', all.length, '珍藏诗稿', '份'],
    ['scan', all.filter((d) => d.status === 'done').length, '已录入文字', '份'],
    ['edit', all.filter((d) => d.status === 'done' && !d.reviewed).length, '等候校对', '份'],
    ['heart', all.filter((d) => d.favorite).length, '心头收藏', '份'],
  ]
    .map(
      ([i, n, label, unit]) =>
        `<div class="stat"><span class="stat-icon">${icon(i)}</span><div><div class="stat-value">${String(n).padStart(2, '0')}<small>${unit}</small></div><div class="stat-label">${label}</div></div></div>`,
    )
    .join('');
  $('#gallery').className =
    `gallery ${state.layout === 'list' ? 'list' : ''} ${state.selecting ? 'selecting' : ''}`;
  $('#gallery').innerHTML = pageDocs
    .map(
      (d) =>
        `<article class="poem-card">${state.selecting ? `<input class="select-check" type="checkbox" aria-label="选择 ${esc(d.title)}" data-id="${esc(d.id)}" ${state.selected.has(d.id) ? 'checked' : ''}>` : ''}<button class="card-open" data-open="${esc(d.id)}" aria-label="阅读 ${esc(d.title)}"><span class="card-image"><img loading="lazy" src="/api/documents/${encodeURIComponent(d.id)}/preview" alt="${esc(d.title)}${d.demo ? '赏读配图' : '原稿预览'}">${d.demo ? '<span class="specimen">赏读示例</span>' : ''}<span class="page-count">${d.demo ? '馆藏配图' : d.pages + ' 页原稿'}</span></span><div class="card-content"><h3 class="card-title">${esc(d.title)}</h3><div class="card-author">${esc([d.era, d.author || '作者待补'].filter(Boolean).join(' · '))}</div><p class="card-excerpt">${esc(
          (d.excerpt || '')
            .replace(/^#+\s*/gm, '')
            .split('\n')
            .filter(Boolean)[0] || '等一页旧笺，化作字里行间。',
        )}</p><div class="card-bottom">${badge(d)}<span>${esc(d.collection)}</span></div></div></button><button class="icon-button favorite-button ${d.favorite ? 'selected' : ''}" data-favorite="${esc(d.id)}" title="${d.favorite ? '取消收藏' : '收藏诗稿'}" aria-label="${d.favorite ? '取消收藏' : '收藏'} ${esc(d.title)}">${icon('heart')}</button></article>`,
    )
    .join('');
  $('#pagination').hidden = pages <= 1;
  $('#pagination').innerHTML =
    `<button class="button secondary" id="previous-page" ${state.listPage === 1 ? 'disabled' : ''}>上一页</button><span>第 ${state.listPage} / ${pages} 页</span><button class="button secondary" id="next-page" ${state.listPage === pages ? 'disabled' : ''}>下一页</button>`;
  $('#previous-page').onclick = () => {
    state.listPage--;
    renderLibrary();
    $('#gallery').scrollIntoView({ block: 'start' });
  };
  $('#next-page').onclick = () => {
    state.listPage++;
    renderLibrary();
    $('#gallery').scrollIntoView({ block: 'start' });
  };
  $$('[data-open]').forEach((b) => (b.onclick = () => openReader(b.dataset.open)));
  $$('[data-favorite]').forEach((b) => (b.onclick = () => toggleFavorite(b.dataset.favorite)));
  $$('.select-check').forEach(
    (b) =>
      (b.onchange = () => {
        b.checked ? state.selected.add(b.dataset.id) : state.selected.delete(b.dataset.id);
        renderSelection();
      }),
  );
  $('#empty').hidden = docs.length > 0;
  $('#empty').innerHTML =
    `${icon(state.query ? 'search' : 'book')}<h3>${state.query ? '还没有找到这句诗。' : state.view === 'library' ? '从第一份诗稿开始。' : state.view === 'review' ? '这一页，已细细读过。' : '这里还静静空着。'}</h3><p>${state.query ? '换一个标题、作者或诗句试试。' : state.view === 'library' ? '把图片拖到这里，或点击下方按钮。<br>照片和扫描件会完整保存在这台电脑。' : state.view === 'review' ? '识别完成的诗稿会来到这里，等你亲自校对。' : '导入或整理后，诗稿就会出现在这里。'}</p>${state.view === 'library' ? '<button class="button primary import-trigger">' + icon('plus') + '导入诗稿</button> <button class="button secondary" id="browse-demo">逛逛示例诗馆</button>' : ''}`;
  if (state.query && state.matches === null) $('#empty').innerHTML = '<h3>正在查找诗稿…</h3>';
  $$('.import-trigger', $('#empty')).forEach((b) => (b.onclick = showImport));
  if ($('#browse-demo')) $('#browse-demo').onclick = () => navigate('demo');
  const pending = all.filter((d) => ['running', 'queued'].includes(d.status));
  $('#queue-note').hidden = state.view !== 'queue';
  $('#queue-note').innerHTML =
    `${pending.length ? `本机正在处理 ${pending.length} 份诗稿；首次运行可能需要下载模型。` : '选择待识别诗稿，开始本地识别。手写识别完成后均需校对。'}<button class="text-button" id="queue-action">${pending.length ? '停止全部任务' : '识别全部待处理'}</button>`;
  $('#queue-action').onclick = async () => {
    if (pending.length) {
      await api('/api/cancel', { method: 'POST', body: { ids: pending.map((d) => d.id) } });
      await refresh();
      toast('已停止任务，原稿已保留');
    } else
      await recognize(
        all.filter((d) => ['new', 'failed', 'interrupted'].includes(d.status)).map((d) => d.id),
      );
  };
  renderSelection();
}
function renderSelection() {
  $('#selection-bar').hidden = !state.selecting;
  $('#select-button').textContent = state.selecting ? '完成管理' : '批量管理';
  $('#selection-count').textContent = `已选 ${state.selected.size} 份`;
  const docs = visibleDocs();
  $('#select-all').checked = docs.length > 0 && docs.every((d) => state.selected.has(d.id));
  $('#selection-trash').textContent = state.view === 'trash' ? '恢复诗稿' : '移入回收站';
}
function leaveReader() {
  if (state.editing) {
    toast('请先保存或取消本次校对，再离开。');
    return false;
  }
  readerRequest++;
  state.reader = null;
  document.body.classList.remove('focus-mode');
  $('#reader-view').hidden = true;
  $('#library-view').hidden = false;
  return true;
}
function navigate(view, collection = '') {
  if (!leaveReader()) return;
  clearTimeout(searchTimer);
  searchRequest++;
  state.matches = null;
  state.listPage = 1;
  state.view = view;
  state.collection = collection;
  state.selected.clear();
  state.query = '';
  state.filter = 'all';
  $('#search').value = '';
  $('#status-filter').value = 'all';
  renderNav();
  renderLibrary();
  window.scrollTo({ top: 0 });
}
async function toggleFavorite(id) {
  const d = state.documents.find((d) => d.id === id);
  await api('/api/documents/' + id, { method: 'PATCH', body: { favorite: !d.favorite } });
  await refresh();
}
function modal(title, subtitle, body) {
  $('#modal-content').innerHTML =
    `<div class="modal-head"><div><h2>${esc(title)}</h2><p>${esc(subtitle)}</p></div><button class="icon-button modal-close" aria-label="关闭对话框">${icon('close')}</button></div><div class="modal-body">${body}</div>`;
  $$('.modal-close').forEach(
    (b) =>
      (b.onclick = () => {
        if (state.importing) {
          toast('正在保存诗稿，请稍候。');
          return;
        }
        $('#modal').close();
      }),
  );
  if (!$('#modal').open) $('#modal').showModal();
}
const collectionOptions = (selected = '未分诗集') =>
  [...new Set(['未分诗集', ...state.collections, selected])]
    .map((n) => `<option ${n === selected ? 'selected' : ''}>${esc(n)}</option>`)
    .join('');
function showImport() {
  if (state.editing) {
    toast('请先保存校对内容。');
    return;
  }
  if (state.importing) {
    toast('正在保存诗稿，请稍候。');
    return;
  }
  state.importCollection = state.collection || '未分诗集';
  modal(
    '让旧笺，有新归处。',
    '支持拖拽、多选文件，或一次导入整个文件夹。',
    `<label class="field">存入诗集<select id="import-collection">${collectionOptions(state.importCollection)}</select></label><div class="import-zone" id="import-zone">${icon('upload')}<h3>把诗稿轻轻放在这里</h3><p>JPG / PNG / WEBP / BMP / PDF · 每份不超过 200 MB</p><button id="choose-files" class="button primary">${icon('plus')}选择多份诗稿</button><button id="choose-folder" class="button secondary">${icon('folder')}选择文件夹</button></div><div class="modal-info">${icon('shield')} 仅导入并保存到本机。点击“开始识别”后才运行本地 MinerU。原图完整保留，手写结果需要你亲自校对。</div><div class="import-status" id="import-status"></div><div class="progress" id="import-progress" hidden><div></div></div><div class="modal-actions" id="import-actions" hidden><button class="button secondary modal-close">稍后整理</button><button class="button primary" id="recognize-imported">开始本地识别</button></div>`,
  );
  $('#import-collection').onchange = (e) => (state.importCollection = e.target.value);
  $('#choose-files').onclick = () => $('#file-input').click();
  $('#choose-folder').onclick = () => $('#folder-input').click();
}
async function importFiles(files) {
  if (state.importing) return;
  files = [...files].filter((f) => /\.(jpe?g|png|webp|bmp|pdf)$/i.test(f.name));
  if (!files.length) {
    toast('请选择 JPG、PNG、WEBP、BMP 或 PDF 文件。');
    return;
  }
  if (!$('#import-status') || !$('#modal').open) showImport();
  state.importing = true;
  const imported = [];
  let duplicates = 0;
  const errors = [];
  $('#choose-files').disabled = true;
  $('#choose-folder').disabled = true;
  $('#import-collection').disabled = true;
  $('#import-progress').hidden = false;
  $('#import-actions').hidden = true;
  try {
    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      $('#import-status').textContent = `正在保存 ${i + 1} / ${files.length}：${f.name}`;
      try {
        const form = new FormData();
        form.append('file', f);
        const result = await api(
          '/api/import?collection=' + encodeURIComponent(state.importCollection),
          { method: 'POST', body: form },
        );
        if (result.duplicate) duplicates++;
        else imported.push(result.document.id);
      } catch (e) {
        errors.push(`${f.name}：${e.message}`);
      }
      $('#import-progress>div').style.width = `${((i + 1) / files.length) * 100}%`;
    }
    state.view = 'library';
    state.collection = '';
    state.reader = null;
    state.editing = false;
    $('#reader-view').hidden = true;
    $('#library-view').hidden = false;
    await refresh();
    $('#import-status').textContent =
      `已保存 ${imported.length} 份${duplicates ? `，${duplicates} 份重复文件已跳过` : ''}${errors.length ? `\n${errors.length} 份未导入：\n${errors.join('\n')}` : '。原稿已安存于本机。'}`;
    $('#import-actions').hidden = false;
    $('#recognize-imported').disabled = !imported.length;
    $('#recognize-imported').onclick = async () => {
      $('#modal').close();
      await recognize(imported);
    };
  } finally {
    state.importing = false;
    if ($('#choose-files')) {
      $('#choose-files').disabled = false;
      $('#choose-folder').disabled = false;
      $('#import-collection').disabled = false;
    }
  }
}
async function recognize(ids, force = false) {
  if (!ids.length) {
    toast('请先选择待识别的诗稿。');
    return;
  }
  if (!state.engine.available) {
    showSettings();
    toast('请先配置本地 MinerU 引擎。');
    return;
  }
  const done = ids.some((id) => state.documents.find((d) => d.id === id)?.has_text);
  if (done && !force) {
    modal(
      '重新识别这些诗稿？',
      '已有校对文字会先存入历史，再由新的识别结果替换。',
      `<div class="modal-info">这次会在本机重新识别 ${ids.length} 份诗稿。原图始终保留。</div><div class="modal-actions"><button class="button secondary modal-close">保留现有文字</button><button class="button primary" id="confirm-reparse">重新识别</button></div>`,
    );
    $('#confirm-reparse').onclick = () => {
      $('#modal').close();
      recognize(ids, true);
    };
    return;
  }
  try {
    const data = await api('/api/recognize', { method: 'POST', body: { ids, force } });
    if (!data.accepted.length) {
      toast('这些诗稿已在队列中，或属于赏读示例。');
      return;
    }
    navigate('queue');
    await refresh();
    toast(`已将 ${data.accepted.length} 份诗稿加入本地识别队列`);
  } catch (e) {
    toast(e.message);
  }
}
function showSettings() {
  const e = state.engine;
  modal(
    '识别与阅读设置',
    '本地识别，纸上的记忆留在自己的电脑。',
    `<div class="modal-info">当前为纯本地版，无云端上传接口。首次识别可能联网下载模型；诗稿在本机处理。手写稿建议“标准”或“精细”档，模糊连笔字仍需人工校对。</div><div class="settings-status">${e.available ? '● 已找到本地 MinerU' : '○ 尚未找到本地 MinerU'}</div><form id="settings-form"><label class="field">MinerU 程序路径<input id="engine-path" placeholder="留空自动查找 mineru-kit.exe" value="${esc(e.executable || '')}"><small>${esc(e.resolved_executable || '运行软件目录中的 install-mineru.ps1 安装本地引擎。')}</small></label><div class="two-fields"><label class="field">识别档位<select id="engine-tier">${[
      ['standard', '标准 · 建议手稿先试用'],
      ['advanced', '精细 · 更慢'],
      ['basic', '基础 · 适合印刷体'],
      ['flash', '快速 · 适合预览'],
    ]
      .map(([v, n]) => `<option value="${v}" ${v === e.tier ? 'selected' : ''}>${n}</option>`)
      .join('')}</select></label><label class="field">模型下载来源<select id="model-source">${[
      ['modelscope', 'ModelScope（国内）'],
      ['huggingface', 'Hugging Face'],
      ['local', '仅使用已下载模型'],
    ]
      .map(
        ([v, n]) => `<option value="${v}" ${v === e.model_source ? 'selected' : ''}>${n}</option>`,
      )
      .join(
        '',
      )}</select></label></div><label class="field-checkbox"><input type="checkbox" id="large-text" ${document.body.classList.contains('large-text') ? 'checked' : ''}>长辈阅读模式 · 放大文字与按钮</label><div class="data-path">诗库存放位置：${esc(e.data_dir)}</div><p><a class="readme-link" href="https://opendatalab.github.io/MinerU/zh/quick_start/" target="_blank" rel="noreferrer">查看 MinerU 官方本地安装说明 ↗</a></p><div class="modal-actions"><button type="button" class="button secondary modal-close">返回</button><button class="button primary" type="submit">保存设置</button></div></form>`,
  );
  $('#settings-form').onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      state.engine = await api('/api/settings', {
        method: 'PUT',
        body: {
          executable: $('#engine-path').value,
          tier: $('#engine-tier').value,
          model_source: $('#model-source').value,
        },
      });
      const large = $('#large-text').checked;
      localStorage.setItem('shijian-large', String(large));
      document.body.classList.toggle('large-text', large);
      $('#modal').close();
      toast(
        state.engine.available
          ? '设置已保存，本地引擎已就绪'
          : '设置已保存；填写的程序路径暂时不可用',
      );
    } catch (e) {
      toast(e.message);
    }
  };
}
function newCollection() {
  modal(
    '新建一本诗集',
    '给相近的诗稿，一个共同的归处。',
    `<form id="collection-form"><label class="field">诗集名称<input id="collection-name" required maxlength="100" placeholder="如：爷爷的旧诗稿、乡居岁月" autofocus></label><div class="modal-actions"><button type="button" class="button secondary modal-close">取消</button><button class="button primary">创建诗集</button></div></form>`,
  );
  $('#collection-form').onsubmit = async (e) => {
    e.preventDefault();
    const name = $('#collection-name').value.trim();
    try {
      await api('/api/collections', { method: 'POST', body: { name } });
      await refresh();
      $('#modal').close();
      navigate('library', name);
      toast('诗集已创建');
    } catch (e) {
      toast(e.message);
    }
  };
}
function selectedIds() {
  if (!state.selected.size) {
    toast('请先勾选诗稿。');
    return [];
  }
  return [...state.selected];
}
function showMove() {
  const ids = selectedIds();
  if (!ids.length) return;
  modal(
    '归入诗集',
    `为 ${ids.length} 份诗稿选择归处。`,
    `<label class="field">诗集<select id="move-to">${collectionOptions()}</select></label><div class="modal-actions"><button class="button secondary modal-close">取消</button><button id="move-confirm" class="button primary">确认归档</button></div>`,
  );
  $('#move-confirm').onclick = async () => {
    const collection = $('#move-to').value;
    try {
      for (const id of ids)
        await api('/api/documents/' + id, { method: 'PATCH', body: { collection } });
      $('#modal').close();
      await refresh();
      toast('诗稿已归入诗集');
    } catch (e) {
      toast(e.message);
    }
  };
}
async function trashSelected() {
  const ids = selectedIds();
  if (!ids.length) return;
  const restoring = state.view === 'trash';
  modal(
    restoring ? '恢复这些诗稿？' : '移入回收站？',
    restoring ? '原图、文字与校对历史会一起恢复。' : '所有内容均会保留，之后可以在回收站恢复。',
    `<div class="modal-info">共 ${ids.length} 份诗稿。</div><div class="modal-actions"><button class="button secondary modal-close">返回</button><button class="button primary" id="trash-confirm">${restoring ? '恢复诗稿' : '移入回收站'}</button></div>`,
  );
  $('#trash-confirm').onclick = async () => {
    try {
      for (const id of ids)
        await api('/api/documents/' + id, { method: 'PATCH', body: { trashed: !restoring } });
      state.selected.clear();
      $('#modal').close();
      await refresh();
      toast(restoring ? '诗稿已恢复' : '已移入回收站');
    } catch (e) {
      toast(e.message);
    }
  };
}
function download(url) {
  const a = document.createElement('a');
  a.href = url;
  a.download = '';
  document.body.append(a);
  a.click();
  a.remove();
}
function showExport(ids) {
  if (!ids?.length) {
    toast('这里还没有可导出的诗稿。');
    return;
  }
  modal(
    '把诗意带走',
    '导出的文字为 UTF-8 编码，可以长久保存。',
    `<label class="field">导出内容<select id="export-format"><option value="txt">纯文字 TXT · 方便阅读</option><option value="md">Markdown · 方便继续整理</option><option value="archive">完整档案 · 原图 + 文字 + 校对历史</option></select></label><div class="modal-info">将导出 ${ids.length} 份诗稿，打包成 ZIP 文件。若需备份整个诗库及分类，请使用侧边栏“备份诗库”。</div><div class="modal-actions"><button class="button secondary modal-close">返回</button><button class="button primary" id="export-confirm">${icon('download')}导出文件</button></div>`,
  );
  $('#export-confirm').onclick = async () => {
    const button = $('#export-confirm');
    button.disabled = true;
    try {
      const result = await api('/api/export', {
        method: 'POST',
        body: { ids, format: $('#export-format').value },
      });
      download(result.url);
      $('#modal').close();
      toast('导出已生成，正在保存 ZIP 文件');
    } catch (e) {
      toast(e.message);
      button.disabled = false;
    }
  };
}
async function backup() {
  modal(
    '给记忆，多留一份底稿。',
    '完整备份包含原图、识别结果、文字、分类和校对历史。',
    `<div class="modal-info">备份保存为 ZIP，建议另存到移动硬盘。恢复方法随压缩包附带；识别引擎和模型不包含在诗库备份内。</div><div class="modal-actions"><button class="button secondary modal-close">返回</button><button class="button primary" id="backup-confirm">${icon('download')}生成完整备份</button></div>`,
  );
  $('#backup-confirm').onclick = async () => {
    const b = $('#backup-confirm');
    b.disabled = true;
    b.textContent = '正在整理备份…';
    try {
      const result = await api('/api/backup', { method: 'POST' });
      download(result.url);
      $('#modal').close();
      toast('完整备份已生成，正在保存');
    } catch (e) {
      toast(e.message);
      b.disabled = false;
      b.textContent = '重试备份';
    }
  };
}
async function openReader(id) {
  if (state.editing) {
    toast('请先保存或取消校对。');
    return;
  }
  const request = ++readerRequest;
  try {
    const doc = await api('/api/documents/' + id);
    if (request !== readerRequest || state.editing) return;
    state.reader = doc;
    state.zoom = 1;
    state.rotation = 0;
    state.page = 1;
    $('#library-view').hidden = true;
    $('#reader-view').hidden = false;
    renderReader();
    window.scrollTo({ top: 0 });
  } catch (e) {
    toast(e.message);
  }
}
function renderReader() {
  const d = state.reader;
  if (!d) return;
  $('#reader-view').innerHTML =
    `<div class="reader-heading"><button class="icon-button" id="reader-back" aria-label="返回诗馆" title="返回诗馆">${icon('back')}</button><div><h1>${esc(d.title)}</h1><div class="reader-subtitle">${esc([d.era, d.author, d.collection].filter(Boolean).join(' · '))}</div></div><div class="reader-actions"><button class="button secondary optional" id="focus-reading">${icon('book')}${document.body.classList.contains('focus-mode') ? '图文对照' : '静心阅读'}</button><button class="button secondary" id="reader-export">${icon('download')}导出</button><button class="button primary" id="edit-button">${icon('edit')}校对文字</button></div></div>${d.error ? `<div class="reader-error">${esc(d.error)}</div>` : ''}<div class="reader-grid"><div class="reader-pane"><div class="pane-toolbar">${icon('image')}<span>${d.demo ? '赏读配图' : '原稿'}</span><span class="spacer"></span><select id="page-select" aria-label="原稿页码">${Array.from({ length: d.pages }, (_, i) => `<option value="${i + 1}" ${i + 1 === state.page ? 'selected' : ''}>第 ${i + 1} / ${d.pages} 页</option>`).join('')}</select><button class="icon-button" id="zoom-out" title="缩小" aria-label="缩小原图">${icon('minus')}</button><span id="zoom-value">${Math.round(state.zoom * 100)}%</span><button class="icon-button" id="zoom-in" title="放大" aria-label="放大原图">${icon('plus')}</button><button class="icon-button" id="rotate" title="旋转" aria-label="旋转原图">${icon('rotate')}</button></div><div class="scan-viewport"><div class="scan-inner"><img id="scan-image" src="/api/documents/${encodeURIComponent(d.id)}/image?page=${state.page}" alt="${esc(d.title)}${d.demo ? '的赏读配图' : '的原稿'}，第${state.page}页"></div></div></div><div class="reader-pane"><div class="pane-toolbar">${icon('book')}<span>诗词正文</span>${badge(d)}<span class="spacer"></span><button class="text-button" id="vertical-toggle">竖排</button><button class="icon-button" id="text-larger" aria-label="放大正文" title="放大正文">A+</button><button class="icon-button" id="copy-text" aria-label="复制文字" title="复制文字">${icon('copy')}</button></div><div class="reader-text" id="reader-text"><div class="poem-display ${!d.text ? 'empty-text' : ''}" id="poem-display">${esc(d.text || '这一页，还在等待被读懂。\n\n点击下方“开始本地识别”，或直接校对录入文字。')}</div><div class="text-foot">${d.demo ? '赏读配图来自博物馆开放馆藏，与预置诗文搭配展示；并非对应诗稿或识别结果。' : '保留识别时的原始字形与换行；不会自动改成通行诗词版本。' + (d.pages > 1 ? ' 多页文件正文按整份文档展示，可在左侧切换原稿页码。' : '')}<br>${d.notes ? '札记：' + esc(d.notes) : ''}</div></div></div></div><div class="reader-bottom"><span class="reader-meta">${esc(d.filename)} · ${d.pages} 页 · ${d.text.replace(/\s/g, '').length} 字</span><div><button class="text-button" id="reader-history">校对历史</button>　<button class="text-button" id="view-raw">原始识别文字</button>　${!d.demo ? `<button class="text-button" id="reader-recognize">${d.text ? '重新识别' : '开始本地识别'}</button>` : ''}</div></div>`;
  $('#reader-back').onclick = () => {
    if (leaveReader()) renderLibrary();
  };
  $('#reader-export').onclick = () => {
    if (state.editing) {
      toast('请先保存校对，再导出。');
      return;
    }
    showExport([d.id]);
  };
  $('#edit-button').onclick = startEditing;
  $('#focus-reading').onclick = () => {
    if (state.editing) {
      toast('请先保存校对。');
      return;
    }
    document.body.classList.toggle('focus-mode');
    renderReader();
  };
  $('#page-select').onchange = (e) => {
    state.page = Number(e.target.value);
    $('#scan-image').src = `/api/documents/${encodeURIComponent(d.id)}/image?page=${state.page}`;
  };
  const applyZoom = () => {
    $('.scan-inner').style.width = `${state.zoom * 100}%`;
    $('#zoom-value').textContent = Math.round(state.zoom * 100) + '%';
    $('#scan-image').style.transform = `rotate(${state.rotation}deg)`;
  };
  $('#zoom-in').onclick = () => {
    state.zoom = Math.min(4, state.zoom + 0.25);
    applyZoom();
  };
  $('#zoom-out').onclick = () => {
    state.zoom = Math.max(0.5, state.zoom - 0.25);
    applyZoom();
  };
  $('#rotate').onclick = () => {
    state.rotation = (state.rotation + 90) % 360;
    applyZoom();
  };
  applyZoom();
  $('#vertical-toggle').onclick = () => {
    const p = $('#poem-display');
    if (!p) return;
    p.classList.toggle('vertical');
    $('#vertical-toggle').textContent = p.classList.contains('vertical') ? '横排' : '竖排';
  };
  $('#text-larger').onclick = () => {
    const p = $('#poem-display');
    if (!p) return;
    let size = parseFloat(getComputedStyle(p).fontSize);
    p.style.fontSize = (size >= 36 ? 20 : size + 4) + 'px';
  };
  $('#copy-text').onclick = async () => {
    try {
      await navigator.clipboard.writeText(state.editing ? $('#edit-text').value : d.text);
      toast('文字已复制');
    } catch {
      toast('复制不可用，可以在校对模式中选中文字复制。');
    }
  };
  $('#reader-history').onclick = showHistory;
  $('#view-raw').onclick = () => {
    if (state.editing) {
      toast('请先保存校对。');
      return;
    }
    modal(
      '最初的字句',
      d.demo ? '这是预置示例文字。' : '以下为 MinerU 的原始结果，独立于人工校对文字保存。',
      `<pre class="raw-text">${esc(d.raw_text || '尚无识别结果。')}</pre>`,
    );
  };
  if ($('#reader-recognize'))
    $('#reader-recognize').onclick = () => {
      if (state.editing) {
        toast('请先保存校对。');
        return;
      }
      recognize([d.id]);
    };
}
function startEditing() {
  const d = state.reader;
  if (['running', 'queued'].includes(d.status)) {
    toast('正在识别，请完成后再校对。');
    return;
  }
  if (state.editing) return;
  state.editing = true;
  $('#edit-button').disabled = true;
  $('#reader-text').innerHTML =
    `<div class="editor-fields"><input id="edit-title" aria-label="诗稿标题" value="${esc(d.title)}" maxlength="200"><input id="edit-author" aria-label="作者" placeholder="作者" value="${esc(d.author)}" maxlength="100"><select id="edit-collection" aria-label="诗集">${collectionOptions(d.collection)}</select><input id="edit-era" aria-label="年代" placeholder="年代 / 写作年份" value="${esc(d.era)}" maxlength="100"></div><textarea class="editor-text" id="edit-text" aria-label="诗词正文" spellcheck="false">${esc(d.text)}</textarea><textarea class="editor-notes" id="edit-notes" aria-label="札记" placeholder="札记：写作时间、字迹疑问，或这首诗的故事…">${esc(d.notes)}</textarea><div class="editor-actions"><button class="button secondary" id="edit-cancel">取消</button><button class="button secondary" id="edit-save">保存草稿</button><button class="button primary" id="edit-reviewed">${icon('check')}保存并完成校对</button></div>`;
  $('#edit-cancel').onclick = () => {
    state.editing = false;
    renderReader();
  };
  $('#edit-save').onclick = () => saveEdit(false);
  $('#edit-reviewed').onclick = () => saveEdit(true);
}
async function saveEdit(reviewed) {
  if (!state.editing || state.saving) return;
  const id = state.reader.id;
  const body = {
    title: $('#edit-title').value.trim(),
    author: $('#edit-author').value.trim(),
    era: $('#edit-era').value.trim(),
    collection: $('#edit-collection').value,
    text: $('#edit-text').value,
    notes: $('#edit-notes').value,
    reviewed,
  };
  if (!body.title) {
    toast('请给诗稿起一个标题。');
    return;
  }
  if (reviewed && !body.text.trim()) {
    toast('请先填写正文，再完成校对。');
    return;
  }
  setEditorSaving(true);
  try {
    const saved = await api('/api/documents/' + id, { method: 'PATCH', body });
    state.reader = saved;
    state.editing = false;
    renderReader();
    toast(reviewed ? '校对已完成，这页诗意已妥善保存' : '草稿已保存，旧版本可在校对历史中查看');
  } catch (e) {
    toast(e.message);
    return;
  } finally {
    setEditorSaving(false);
  }
  try {
    await refresh();
  } catch {
    toast('校对已保存，列表暂时无法刷新；请稍后重试。');
  }
}

function setEditorSaving(saving) {
  state.saving = saving;
  $$(
    '.editor-fields input,.editor-fields select,#edit-text,#edit-notes,.editor-actions button',
  ).forEach((control) => (control.disabled = saving));
  if ($('#edit-save')) $('#edit-save').textContent = saving ? '正在保存…' : '保存草稿';
}
async function showHistory() {
  if (state.editing) {
    toast('请先保存本次校对。');
    return;
  }
  const id = state.reader.id,
    history = await api('/api/documents/' + id + '/revisions');
  modal(
    '字句之间，有迹可循。',
    '恢复旧版本前，当前版本也会保存到历史。',
    history.length
      ? history
          .map(
            (h) =>
              `<div class="history-item"><time>${esc(new Date(h.created_at).toLocaleString('zh-CN'))}</time><button class="text-button" data-restore="${h.id}">恢复此版本</button><h3>${esc(h.title)}</h3><pre>${esc(h.text || '（尚无文字）')}</pre></div>`,
          )
          .join('')
      : '<div class="modal-info">还没有修改历史。第一次保存校对时，会自动留下此前版本。</div>',
  );
  $$('[data-restore]').forEach(
    (b) =>
      (b.onclick = async () => {
        const h = history.find((h) => h.id === Number(b.dataset.restore));
        try {
          state.reader = await api('/api/documents/' + id, {
            method: 'PATCH',
            body: {
              title: h.title,
              author: h.author,
              era: h.era,
              text: h.text,
              notes: h.notes,
              reviewed: false,
            },
          });
          $('#modal').close();
          await refresh();
          renderReader();
          toast('已恢复旧版本，请重新确认校对');
        } catch (e) {
          toast(e.message);
        }
      }),
  );
}
// Bind all public controls; errors are also surfaced for keyboard-triggered promises.
$$('.import-trigger').forEach((b) => (b.onclick = showImport));
$$('.nav-item[data-view]').forEach((b) => (b.onclick = () => navigate(b.dataset.view)));
$('.brand').onclick = (e) => {
  e.preventDefault();
  navigate('library');
};
$('#art-sources').onclick = async () => {
  const sources = await api('/static/art/sources.json');
  modal(
    '画中来处',
    '配图选自 The Met 开放馆藏，随软件保存，离线也能欣赏。',
    sources
      .map(
        (a) =>
          `<div class="history-item"><strong>${esc(a.title)}</strong><p style="font-size:11px;color:#899777">${esc(a.rights)}</p><a class="readme-link" href="${esc(a.source)}" target="_blank" rel="noreferrer">查看馆藏原页 ↗</a></div>`,
      )
      .join(''),
  );
};
$('#settings-button').onclick = showSettings;
$('#new-collection').onclick = newCollection;
$('#backup-button').onclick = backup;
$('#search').oninput = (e) => searchDocuments(e.target.value);
$('#status-filter').onchange = (e) => {
  state.filter = e.target.value;
  state.listPage = 1;
  renderLibrary();
};
$('#sort').onchange = (e) => {
  state.sort = e.target.value;
  state.listPage = 1;
  renderLibrary();
};
$('#grid-button').onclick = () => {
  state.layout = 'grid';
  $('#grid-button').classList.add('active');
  $('#list-button').classList.remove('active');
  renderLibrary();
};
$('#list-button').onclick = () => {
  state.layout = 'list';
  $('#grid-button').classList.remove('active');
  $('#list-button').classList.add('active');
  renderLibrary();
};
$('#select-button').onclick = () => {
  state.selecting = !state.selecting;
  state.selected.clear();
  renderLibrary();
};
$('#select-all').onchange = (e) => {
  visibleDocs().forEach((d) =>
    e.target.checked ? state.selected.add(d.id) : state.selected.delete(d.id),
  );
  renderLibrary();
};
$('#selection-recognize').onclick = () => {
  const ids = selectedIds();
  if (ids.length) recognize(ids);
};
$('#selection-move').onclick = showMove;
$('#selection-trash').onclick = trashSelected;
$('#selection-export').onclick = () => showExport(selectedIds());
$('#export-button').onclick = () => showExport(visibleDocs().map((d) => d.id));
$('#file-input').onchange = (e) => {
  importFiles(e.target.files);
  e.target.value = '';
};
$('#folder-input').onchange = (e) => {
  importFiles(e.target.files);
  e.target.value = '';
};
$('#modal').addEventListener('cancel', (e) => {
  if (state.importing) {
    e.preventDefault();
    toast('正在保存诗稿，请稍候。');
  }
});
$('#modal').addEventListener('click', (e) => {
  if (e.target === $('#modal') && !state.importing) $('#modal').close();
});
let dragDepth = 0;
document.addEventListener('dragenter', (e) => {
  if (!e.dataTransfer.types.includes('Files')) return;
  e.preventDefault();
  dragDepth++;
  if (!state.importing) $('#drop-overlay').hidden = false;
});
document.addEventListener('dragover', (e) => {
  if (e.dataTransfer.types.includes('Files')) e.preventDefault();
});
document.addEventListener('dragleave', () => {
  dragDepth--;
  if (dragDepth <= 0) $('#drop-overlay').hidden = true;
});
document.addEventListener('drop', (e) => {
  e.preventDefault();
  dragDepth = 0;
  $('#drop-overlay').hidden = true;
  if (state.editing) {
    toast('请先保存校对，再导入诗稿。');
    return;
  }
  if (e.dataTransfer.files.length) importFiles(e.dataTransfer.files);
});
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's' && state.editing) {
    e.preventDefault();
    saveEdit(false);
  }
  if (e.key === 'Escape' && state.reader && !$('#modal').open && !state.editing) {
    leaveReader();
    renderLibrary();
  }
});
window.addEventListener('beforeunload', (e) => {
  if (state.editing || state.importing) {
    e.preventDefault();
    e.returnValue = '';
  }
});
window.addEventListener('unhandledrejection', (e) => {
  console.error(e.reason);
  toast(e.reason?.message || '操作未完成，请稍后再试。');
  e.preventDefault();
});
document.body.classList.toggle('large-text', localStorage.getItem('shijian-large') === 'true');
(async () => {
  try {
    await refresh();
    if (!state.documents.some((d) => !d.demo && !d.trashed)) {
      state.view = 'demo';
      renderNav();
      renderLibrary();
    }
  } catch (e) {
    $('#gallery').innerHTML = '';
    $('#empty').hidden = false;
    $('#empty').innerHTML = `<h3>暂时无法连接诗库</h3><p>${esc(e.message)}，请重新启动拾笺。</p>`;
  }
})();
setInterval(() => pollTasks().catch(() => {}), 2500);
