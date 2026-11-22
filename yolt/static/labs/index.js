// Labs: experimental features built on semantic search. The server lists them (GET /api/v1/labs, from
// yolt/labs/__init__.py) with whether each is switched on (the labs_disabled setting) and new (badged "New").
// Each lab is a module here exporting any of these hooks, which app.js calls through install()'s result:
//   page(params, view, ctx)       the page at /labs/<id>: one of the tools on the Compilations tab
//   clipActions(clip, ctx) -> html buttons on a clip's page
//   clip(clip, view, ctx)         fills the labs slot under a clip's page
//   home(view, ctx)               fills the labs slot on the home page
// To delete a lab, remove its line below and its module (and see yolt/labs/__init__.py).

const MODULES = {
  thread: 'thread.js',
  foryou: 'foryou.js',
};

const names = (value) => String(value ?? '').split(',').map((s) => s.trim()).filter(Boolean);

export async function install(app) {
  const { esc, api } = app;
  const version = new URL(import.meta.url).search;
  const style = document.createElement('link');
  style.rel = 'stylesheet';
  style.href = `/static/labs/labs.css${version}`;
  document.head.append(style);

  const modules = {};
  await Promise.all(Object.entries(MODULES).map(async ([id, file]) => {
    try {
      modules[id] = (await import(`./${file}${version}`)).default;
    } catch (error) {
      console.warn(`lab ${id} didn't load`, error); // one broken lab doesn't take the others down
    }
  }));

  let listing = [];
  const info = (id) => listing.find((lab) => lab.id === id);
  const badge = (lab) => (lab?.new ? '<span class="new-badge" title="New: one of the labs">New</span>' : '');
  const active = (hook) => listing.filter((lab) => lab.enabled && modules[lab.id]?.[hook]);

  // What a lab gets: the app's helpers, plus its own name and badge.
  const ctxFor = (lab) => ({
    ...app,
    lab,
    badge: badge(lab),
    heading: () => toolsHeading(lab.id), // a page's top: Compilations, and its tools as tabs
    compilationForm,
    bindCompilationForm,
  });

  // Save clips, in order, as a new compilation (POST /compilations).
  function compilationForm(title) {
    return `<form class="search-form lab-save">
      <input type="text" name="title" maxlength="200" value="${esc(title)}" aria-label="Compilation title" required>
      <button type="submit" class="box-button small">Save as compilation</button>
      <span class="note" role="status"></span></form>`;
  }
  function bindCompilationForm(form, clipIds) {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const note = form.querySelector('.note');
      const button = form.querySelector('button');
      const ids = clipIds();
      if (!ids.length) { note.textContent = 'Nothing to save.'; return; }
      button.disabled = true;
      note.textContent = 'Saving…';
      const body = { title: form.elements.title.value.trim(), items: ids.map((id) => ({ clip_id: id })) };
      const { ok, data } = await app.apiRaw('/compilations', { method: 'POST', body: JSON.stringify(body) });
      button.disabled = false;
      note.innerHTML = ok ? `Saved as compilation ${esc(data.compilation_id)} ·
        <a href="/api/v1/compilations/${esc(data.compilation_id)}" target="_blank">view in the API</a>`
        : esc(app.problemText(data) || 'Saving failed.');
    });
  }

  // The Compilations tab holds every lab with a page, as tools; it's hidden when they're all switched off.
  const tools = () => active('page');
  let lastTool = null; // the Compilations tab reopens the tool last used

  // Its tab under Episodes (app.js libraryTabs): only while a tool is switched on, badged while one is new.
  function compilationsTab(current) {
    if (!tools().length) return '';
    return `<a href="/compilations" data-link${current ? ' aria-current="page"' : ''}>Compilations${
      tools().some((lab) => lab.new) ? ` ${badge({ new: true })}` : ''}</a>`;
  }
  const libraryTabs = () => app.libraryTabs?.('compilations') ?? '';

  function toolsHeading(current) {
    return `${libraryTabs()}<h1 class="page-title">Compilations</h1>
      <nav class="tool-tabs" aria-label="Compilation tools">${tools().map((lab) => `<a href="/labs/${esc(lab.id)}" data-link
        ${lab.id === current ? 'aria-current="page"' : ''}>${esc(lab.name)} ${badge(lab)}</a>`).join('')}</nav>`;
  }

  async function refresh() {
    listing = (await api('/labs')).data;
  }
  await refresh().catch(() => {});

  // Settings → Labs (not shown for now: app.js showSettings): each checkbox keeps the form's `labs_disabled` field (the unchecked ones) up to date.
  document.addEventListener('change', (event) => {
    if (!event.target.matches('[data-lab-toggle]')) return;
    const form = event.target.form;
    const off = [...form.querySelectorAll('[data-lab-toggle]')].filter((box) => !box.checked).map((box) => box.value);
    form.elements.labs_disabled.value = off.join(',');
  });

  const run = (hook, ...args) => {
    for (const lab of active(hook)) {
      Promise.resolve().then(() => modules[lab.id][hook](...args, ctxFor(lab)))
        .catch((error) => console.warn(`lab ${lab.id}`, error));
    }
  };

  return {
    refresh,
    compilationsTab,
    async page(id, params, view) {
      const lab = info(id);
      if (!lab || !modules[id]?.page) throw new Error('There is no such page.');
      app.setTitle(lab.name);
      lastTool = id;
      if (!lab.enabled) {
        app.main.innerHTML = `${ctxFor(lab).heading()}<div class="empty-state"><div>
          <p>${esc(lab.name)} is switched off, by the <code>labs_disabled</code> setting (<code>PUT /api/v1/settings</code>) or YOLT_LABS_DISABLED.</p></div></div>`;
        return;
      }
      await modules[id].page(params, view, ctxFor(lab));
    },
    // /compilations: the tool last used, else the first.
    compilations() {
      const list = tools();
      const lab = list.find((l) => l.id === lastTool) || list[0];
      if (lab) { app.navigate(`/labs/${lab.id}`, { replace: true }); return; }
      app.main.innerHTML = `${libraryTabs()}<h1 class="page-title">Compilations</h1><div class="empty-state"><div>
        <p>The compilation tools (${listing.filter((l) => modules[l.id]?.page).map((l) => esc(l.name)).join(', ')
  || 'none'}) are switched off, by the <code>labs_disabled</code> setting (<code>PUT /api/v1/settings</code>) or YOLT_LABS_DISABLED.</p></div></div>`;
    },
    clipActions: (clip) => active('clipActions').map((lab) => modules[lab.id].clipActions(clip, ctxFor(lab))).join(''),
    clip: (clip, view) => run('clip', clip, view),
    home: (view) => run('home', view),
    settings(config) {
      if (!listing.length) return '';
      const locked = config.env_overrides.includes('labs_disabled');
      // Only labs this server has: a deleted lab's name left in the setting would make a save be refused.
      const off = names(config.values.labs_disabled).filter((id) => info(id));
      return `<fieldset><legend>Labs</legend>
        <input type="hidden" name="labs_disabled" value="${esc(off.join(','))}" ${locked ? 'disabled' : ''}>
        ${listing.map((lab) => `<label class="field lab-toggle">
          <span><input type="checkbox" data-lab-toggle value="${esc(lab.id)}" ${off.includes(lab.id) ? '' : 'checked'}
            ${locked ? 'disabled' : ''}> ${esc(lab.name)} ${badge(lab)}</span>
          <span class="hint">${esc(lab.summary)}</span></label>`).join('')}
        ${locked ? '<p class="hint lock">Set by YOLT_LABS_DISABLED.</p>' : ''}
        <p class="hint">Experimental features built on search by meaning. Switch one off to hide it everywhere.</p>
      </fieldset>`;
    },
  };
}
