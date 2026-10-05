const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = readFileSync(path.join(__dirname, '../static/app.js'), 'utf8').split(
  '// Bind all public controls',
)[0];

function harness() {
  const nodes = new Map();
  const node = (selector) => {
    if (!nodes.has(selector))
      nodes.set(selector, {
        value: '',
        disabled: false,
        hidden: false,
        textContent: '',
        innerHTML: '',
        classList: {
          add() {},
          remove() {},
          toggle() {},
          contains() {
            return false;
          },
        },
        scrollIntoView() {},
        querySelectorAll() {
          return [];
        },
      });
    return nodes.get(selector);
  };
  const controls = [
    '#edit-title',
    '#edit-author',
    '#edit-era',
    '#edit-collection',
    '#edit-text',
    '#edit-notes',
    '#edit-save',
    '#edit-reviewed',
    '#edit-cancel',
  ];
  const context = vm.createContext({
    console,
    setTimeout: () => 0,
    clearTimeout() {},
    window: { scrollTo() {} },
    document: {
      querySelector: node,
      querySelectorAll: (selector) =>
        selector.includes('.editor-fields input') ? controls.map(node) : [],
      body: node('body'),
    },
  });
  vm.runInContext(source, context);
  const run = (code) => vm.runInContext(code, context);
  run('globalThis.messages=[];toast=message=>messages.push(message);');
  return { node, run };
}

function prepareEditor(h) {
  for (const [selector, value] of Object.entries({
    '#edit-title': '詩稿',
    '#edit-author': '祖父',
    '#edit-era': '',
    '#edit-collection': '家藏',
    '#edit-text': '需要保存的校對',
    '#edit-notes': '札記',
  }))
    h.node(selector).value = value;
  h.run(`state.reader={id:'one',text:'舊文字',updated_at:'1'};state.editing=true;
         globalThis.renderCount=0;renderReader=()=>renderCount++;refresh=async()=>{};`);
}

test('idle polling discovers edits from another window', async () => {
  const h = harness();
  h.run(`state.reader={id:'one',status:'done',text:'old',updated_at:'1'};
    state.documents=[{...state.reader}];globalThis.syncs=0;
    api=async()=>({changed:true});
    refresh=async()=>{syncs++;};`);
  await h.run('pollTasks()');
  assert.equal(h.run('syncs'), 1);
});

test('search keeps previous matches while the next request is pending', () => {
  const h = harness();
  h.run(`state.documents=[{id:'one',created_at:'1'}];state.matches=new Set(['one']);
    renderLibrary=()=>{};searchDocuments('new query');`);
  assert.equal(h.run('visibleDocs().length'), 1);
  assert.equal(h.run('state.searching'), true);
});

test('batch updates continue after failure and retry only failed items', async () => {
  const h = harness();
  h.run(`globalThis.calls=[];globalThis.refreshes=0;
    api=async u=>{calls.push(u);if(u.endsWith('/b'))throw Error('sample failure');};
    refresh=async()=>{refreshes++};`);
  const result = await h.run("updateSelection(['a','b','c'],{collection:'诗集'})");
  assert.deepEqual(Array.from(result.succeeded), ['a','c']);
  assert.deepEqual(Array.from(result.failed, x=>x.id), ['b']);
  assert.equal(h.run('refreshes'), 1);
  assert.deepEqual(Array.from(h.run('state.selected')), ['b']);
});

test('history replacement carries the version seen before confirmation', async () => {
  const h = harness();
  h.run(`state.reader={id:'one',updated_at:'1'};globalThis.sent=null;
    api=async(u,o)=>{sent=o.body;throw Error('version conflict');};`);
  await h.run("restoreRevision('one',{title:'old',text:'old'},'1')");
  assert.equal(h.run('sent.expected_updated_at'), '1');
  assert.equal(h.run('state.reader.updated_at'), '1');
});

test('page confirmation rejects a version different from the displayed text', async () => {
  const h = harness();
  h.run(`state.reader={id:'one',text:'旧正文',text_revision:1};
    api=async()=>({text_revision:2,text_version:'new',pages:[]});
    modal=()=>{throw Error('must not show stale text with new version')};`);
  await h.run('editPageNote()');
  assert.match(h.run('messages.at(-1)'), /正文已更新/);
});

test('sync updates page confirmation even when body has not changed', async () => {
  const h = harness();
  h.run(`state.reader={id:'one',text:'same',text_revision:1,updated_at:'1'};
    state.documents=[{...state.reader}];renderNav=()=>{};renderLibrary=()=>{};
    api=async u=>u==='/api/state'
      ?{documents:[{id:'one',updated_at:'1'}],collections:[],engine:{},session:'s'}
      :{text_revision:1,pages:[{page:1,start_line:1,reviewed:1,stale:false}]};`);
  await h.run('refresh()');
  assert.equal(h.run('state.pageNotes.pages[0].reviewed'),1);
  assert.equal(h.run('state.reader.text'),'same');
});

test('saving locks input, rejects duplicate submissions, and saves the displayed draft', async () => {
  const h = harness();
  prepareEditor(h);
  h.run(`globalThis.calls=0;api=async(url,options)=>{
    if(options.method==='PUT') return {draft_version:'v1'};
    calls++;
    globalThis.sent=options.body;return new Promise(resolve=>globalThis.finish=resolve);};`);
  const save = h.run('saveEdit(false)');
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(h.node('#edit-text').disabled, true);
  assert.equal(h.node('#edit-title').disabled, true);
  assert.equal(h.node('#edit-cancel').disabled, true);
  if (!h.node('#edit-text').disabled) h.node('#edit-text').value += '後輸入的字';
  await h.run('saveEdit(true)');
  assert.equal(h.run('calls'), 1);
  h.run(`finish({id:'one',...sent,updated_at:'2'})`);
  await save;
  assert.equal(h.run('state.reader.text'), h.node('#edit-text').value);
  assert.equal(h.run('state.editing'), false);
  assert.equal(h.run('state.saving'), false);
  assert.equal(h.run('renderCount'), 1);
});

test('failed save keeps the draft editable and never redraws it', async () => {
  const h = harness();
  prepareEditor(h);
  h.run(`api=async()=>{throw Error('disk error');};`);
  await h.run('saveEdit(false)');
  assert.equal(h.node('#edit-text').value, '需要保存的校對');
  assert.equal(h.node('#edit-text').disabled, false);
  assert.equal(h.run('state.editing'), true);
  assert.equal(h.run('state.saving'), false);
  assert.equal(h.run('renderCount'), 0);
});

test('recovery writes are serialized and a formal save waits for them', async () => {
  const h = harness();
  prepareEditor(h);
  h.run(`globalThis.calls=[];api=async(url,options)=>{
    calls.push(options.method);
    if(options.method==='PUT' && calls.length===1) return new Promise(resolve=>globalThis.finishDraft=resolve);
    if(options.method==='PUT') return {draft_version:'v2'};
    return {id:'one',...options.body,updated_at:'2'};
  };`);
  const draft = h.run('persistRecoveryDraft()');
  await Promise.resolve();
  await Promise.resolve();
  const save = h.run('saveEdit(false)');
  assert.deepEqual(Array.from(h.run('calls')), ['PUT']);
  h.run('finishDraft({draft_version:"v1"})');
  await draft;
  await save;
  assert.deepEqual(Array.from(h.run('calls')), ['PUT', 'PUT', 'PATCH']);
  assert.equal(h.run('state.editing'), false);
});

test('immediate failed formal save has already persisted the latest keystrokes', async () => {
  const h = harness();
  prepareEditor(h);
  h.node('#edit-text').value='刚输入且尚未自动保存';
  h.run(`api=async(url,options)=>{
    if(options.method==='PUT'){globalThis.recovery=options.body;return {draft_version:'v1'}};
    throw Error('conflict');
  };`);
  await h.run('saveEdit(false)');
  assert.equal(h.run('recovery.text'),'刚输入且尚未自动保存');
  assert.equal(h.run('state.editing'),true);
});

test('failed recovery write keeps editor content and can retry', async () => {
  const h = harness();
  prepareEditor(h);
  h.run(`api=async()=>{throw Error('disk full')};`);
  assert.equal(await h.run('persistRecoveryDraft()'), false);
  assert.equal(h.node('#edit-text').value, '需要保存的校對');
  h.run('api=async()=>({})');
  assert.equal(await h.run('persistRecoveryDraft()'), true);
});

test('list refresh failure after successful save is not reported as a failed save', async () => {
  const h = harness();
  prepareEditor(h);
  h.run(`api=async(url,options)=>({id:'one',...options.body});
         refresh=async()=>{throw Error('offline');};`);
  await h.run('saveEdit(false)');
  assert.equal(h.run('state.reader.text'), '需要保存的校對');
  assert.equal(h.run('state.editing'), false);
  assert.match(h.run('messages.at(-1)'), /校对已保存/);
});

test('completed task refreshes an open reader and unlocks proofreading', async () => {
  const h = harness();
  h.run(`state.reader={id:'one',status:'running',text:'',updated_at:'1'};
    state.documents=[{...state.reader}];globalThis.renders=0;
    renderReader=()=>renders++;renderNav=()=>{};renderLibrary=()=>{};
    api=async url=>url==='/api/tasks/status'
      ?{documents:[{id:'one',status:'done',excerpt:'新識別',updated_at:'2'}],engine:{}}
      :{id:'one',status:'done',text:'新識別',updated_at:'2'};`);
  await h.run('pollTasks()');
  assert.equal(h.run('state.reader.status'), 'done');
  assert.equal(h.run('state.reader.text'), '新識別');
  assert.equal(h.run('renders'), 1);
});

test('a detail response never replaces a draft started while the request was in flight', async () => {
  const h = harness();
  h.run(`state.reader={id:'one',status:'done',text:'舊文字',updated_at:'1'};
    state.documents=[{id:'one',status:'done',updated_at:'2'}];
    renderReader=()=>{throw Error('must not replace draft');};
    api=()=>new Promise(resolve=>globalThis.finish=resolve);`);
  const refresh = h.run('refreshReader()');
  h.run(`state.editing=true;finish({id:'one',text:'伺服器文字',updated_at:'2'});`);
  await refresh;
  assert.equal(h.run('state.reader.text'), '舊文字');
});

test('detail refresh is retried after the last task has finished', async () => {
  const h = harness();
  h.run(`state.lastSync=Date.now();state.reader={id:'one',status:'running',updated_at:'1'};
    state.documents=[{id:'one',status:'done',updated_at:'2'}];
    renderReader=()=>{};
    api=async()=>({id:'one',status:'done',text:'完成',updated_at:'2'});`);
  await h.run('pollTasks()');
  assert.equal(h.run('state.reader.text'), '完成');
});

test('slow task polling never overlaps another polling request', async () => {
  const h = harness();
  h.run(`state.documents=[{id:'one',status:'running',updated_at:'1'}];
    globalThis.calls=0;api=()=>{calls++;return new Promise(resolve=>globalThis.finish=resolve);};`);
  const first = h.run('pollTasks()');
  await h.run('pollTasks()');
  assert.equal(h.run('calls'), 1);
  h.run(`finish({documents:[{id:'one',status:'running',updated_at:'1'}],engine:{}});`);
  await first;
  assert.equal(h.run('state.polling'), false);
});

test('trash includes examples and ordinary library still excludes them', () => {
  const h = harness();
  h.run(`state.documents=[{id:'demo-1',demo:1,trashed:1,created_at:'1'},
                         {id:'one',demo:0,trashed:1,created_at:'1'}];state.view='trash';`);
  assert.equal(h.run('visibleDocs().length'), 2);
  h.run(`state.documents.forEach(d=>d.trashed=0);state.view='library';`);
  assert.equal(h.run('visibleDocs().length'), 1);
  h.run(`state.view='demo';`);
  assert.equal(h.run('visibleDocs()[0].id'), 'demo-1');
});

test('late search results cannot replace a newer query', async () => {
  const h = harness();
  h.run(
    `globalThis.requests=[];api=()=>new Promise(resolve=>requests.push(resolve));state.query='第一句';`,
  );
  const first = h.run('refreshSearch()');
  h.run(`state.query='第二句';`);
  const second = h.run('refreshSearch()');
  h.run(`requests[1]({ids:['two']});`);
  await second;
  h.run(`requests[0]({ids:['one']});`);
  await first;
  assert.equal(h.run('state.matches.has("two")'), true);
  assert.equal(h.run('state.matches.has("one")'), false);
});

test('gallery paginates rendering while select-all and export retain all results', () => {
  const h = harness();
  h.run(`state.documents=Array.from({length:100},(_,i)=>({id:String(i),title:'詩稿',author:'',
    era:'',collection:'家藏',excerpt:'正文',demo:0,trashed:0,status:'done',pages:1,created_at:'1'}));
    renderLibrary();`);
  assert.equal((h.node('#gallery').innerHTML.match(/class="poem-card"/g) || []).length, 48);
  assert.equal(h.run('visibleDocs().length'), 100);
  assert.equal(h.node('#pagination').hidden, false);
  h.run('state.listPage=3;renderLibrary();');
  assert.equal((h.node('#gallery').innerHTML.match(/class="poem-card"/g) || []).length, 4);
});
