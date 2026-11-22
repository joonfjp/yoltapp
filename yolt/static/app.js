// Yolt web UI. Vanilla JS, no build step. Everything goes through /api/v1 (see /api/docs).
// Routes:
//   /                   recently clipped
//   /r/:id              a run (a video + instructions) and its clips
//   /c/:slug            one clip (the shareable link)
//   /search             search every clip (?q, mode, unused, like, theme; themes=1 opens the themes panel, k, sort)
//   /themes             the themes panel's old address: opens it on /search
//   /subscriptions      followed channels and the long videos found on them (?kind, unclipped, channel)
//   /episodes           every run, newest first, as the queue has them (?status)
//   /saved              clips saved with + (kept in this browser)
//   /tv                 clips playing one after another, like a channel (tv.js; ?q, usage and /clips' filters)
//   /settings           service + clipping settings
//   /compilations       the compilation tools (each lab with a page): opens the one last used
//   /labs/:id           a lab's page, a tab under Compilations (labs/index.js)

const $ = (selector, root = document) => root.querySelector(selector);
const main = $('#main');
const POLL_MS = 2500;
const EMBED_RETRY_MS = 30000; // a run page's re-check while the embeddings server is failing

const state = {
  // What the worker is doing (GET /queue): the processing run in full, the queued ones by id, next first.
  queue: { processing: [], queued: [] },
  queueLoaded: false,
  view: null, // { name, id, data }; a new object per navigation
};

// Whether `view` (state.view when a page started loading) is still the page shown. Comparing the object,
// not just its name, keeps a slow response from a page already left (even the same page with other
// params) from drawing over the current one.
const stillOn = (view, name) => state.view === view && view.name === name;

// Labs: experimental features, one module each in /static/labs (see labs/index.js and yolt/labs). They hook
// in only where `labs.` is called. Until they load, or if they can't, these do nothing.
let labs = {
  page: () => { throw new Error('There is no such page.'); },
  compilations: () => { throw new Error("The compilation tools didn't load."); },
  refresh: async () => {}, clipActions: () => '', clip: () => {}, home: () => {}, settings: () => '',
  compilationsTab: () => '',
};

// --- helpers -----------------------------------------------------------------

const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

// Like api(), but returns {ok, status, data} instead of throwing, for callers that handle specific problems.
async function apiRaw(path, options = {}) {
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    headers: options.body ? { 'Content-Type': 'application/json' } : undefined,
  });
  return { ok: response.ok, status: response.status, data: await response.json().catch(() => null) };
}

// Channel links come from yt-dlp: only ever link to https ones (never `javascript:` and the like).
const httpsUrl = (url) => (/^https:\/\//i.test(String(url ?? '')) ? url : '');

const problemText = (data) => data?.errors?.map((e) => e.message).join(' ') || data?.detail || data?.title || '';

async function api(path, options = {}) {
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    headers: options.body ? { 'Content-Type': 'application/json' } : undefined,
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    // application/problem+json: every problem is listed, each with a message.
    const message = data?.errors?.map((e) => e.message).join(' ') || data?.detail || data?.title;
    throw new Error(message || `Request failed (${response.status})`);
  }
  return data;
}

// A More button whose page didn't load: usable again, and saying why.
function moreFailed(button, error) {
  button.disabled = false;
  button.dataset.failed = '1'; // scrolling doesn't press it again; pressing it does
  if (!button.isConnected) return;
  let note = button.parentElement.querySelector('.more-error');
  if (!note) {
    note = document.createElement('p');
    note.className = 'error-text more-error';
    note.setAttribute('role', 'alert');
    button.after(note);
  }
  note.textContent = `Couldn’t load more: ${error?.message || 'the request failed'}`;
}

const isActive = (run) => run.status === 'queued' || run.status === 'processing';
const queueBusy = () => state.queue.processing.length > 0 || state.queue.queued.length > 0;
const queueKey = () => `${state.queue.processing.map((r) => r.id)}|${state.queue.queued.map((q) => q.id)}`;
const processingRun = (id) => state.queue.processing.find((r) => r.id === id);
const queuePosition = (id) => state.queue.queued.findIndex((q) => q.id === id); // -1: not queued

const RUN_STAGES = { fetching: 'Fetching info', downloading: 'Downloading', transcribing: 'Transcribing',
  captions: 'Loading captions', identifying: 'Finding highlights' };

// How far a run has got, in a few words: its stage while processing, its place while queued.
function runStage(run) {
  const live = processingRun(run.id);
  if (live) {
    const stage = RUN_STAGES[live.stage];
    if (!stage) return 'Starting…';
    return live.stage === 'downloading' && live.progress != null ? `${stage} ${Math.round(live.progress * 100)}%` : `${stage}…`;
  }
  const position = queuePosition(run.id);
  if (position === 0) return state.queue.processing.length ? 'Up next' : 'Starting…';
  if (position > 0) return `Queued · ${ordinal(position + 1)}`;
  return run.status === 'queued' ? 'Queued' : 'Clipping…';
}

// Bring a list's run-like objects ({id, status, ...}) up to date with the queue. Returns the ones that
// were going and have left the queue since: they ended (ready, failed or cancelled), and how is a
// request each, once per run (see endedRuns).
function followQueue(runs) {
  const ended = [];
  if (!state.queueLoaded) return ended;
  for (const run of runs) {
    if (!run || !isActive(run)) continue;
    if (processingRun(run.id)) run.status = 'processing';
    else if (queuePosition(run.id) >= 0) run.status = 'queued';
    else ended.push(run);
  }
  return ended;
}

// How each of these ended runs ended (the runs themselves, newly fetched; null where that failed).
const endedRuns = (runs) => Promise.all(runs.map((r) => api(`/runs/${r.id}`).catch(() => null)));

// A YouTube thumbnail for an episode's `source`, the small one (as the API serves found videos').
// `hqdefault` (480 wide, letterboxed to 4:3: a 16:9 .thumb crops the bars off) for a bigger picture.
const videoThumb = (source, size = 'mqdefault') => (source?.platform === 'youtube' && /^[\w-]{11}$/.test(source.video_id || '')
  ? `https://i.ytimg.com/vi/${source.video_id}/${size}.jpg` : '');

const keyedHtml = new WeakMap(); // an element made by syncChildren -> the markup it was made from

// Make `container`'s children one element per item, keeping (untouched) each one whose markup is the
// same as before, so a refresh replaces only what changed: hover, focus and loaded images stay put.
function syncChildren(container, items, keyOf, htmlOf) {
  const focused = container.contains(document.activeElement) ? document.activeElement : null;
  const focusedKey = focused?.closest('[data-key]')?.dataset.key;
  const focusedClass = focused?.classList[0];
  const old = new Map([...container.children].map((el) => [el.dataset.key, el]));
  const maker = document.createElement('div'); // made in this document, so cached thumbnails show at once
  const els = items.map((item) => {
    const key = String(keyOf(item));
    const html = htmlOf(item);
    const el = old.get(key);
    if (el && keyedHtml.get(el) === html) return el;
    maker.innerHTML = html;
    const fresh = maker.firstElementChild;
    fresh.dataset.key = key;
    keyedHtml.set(fresh, html);
    return fresh;
  });
  const current = [...container.children];
  if (els.length === current.length && els.every((el, i) => el.dataset.key === current[i].dataset.key)) {
    els.forEach((el, i) => { if (el !== current[i]) current[i].replaceWith(el); }); // same items: swap the changed
  } else if (current.every((el, i) => els[i] === el)) {
    container.append(...els.slice(current.length)); // the next page
  } else {
    container.replaceChildren(...els);
  }
  if (focused && !focused.isConnected && focusedKey != null) {
    const item = $(`[data-key="${CSS.escape(focusedKey)}"]`, container);
    (focusedClass && item?.querySelector(`.${CSS.escape(focusedClass)}`) || item?.querySelector('a, button'))
      ?.focus({ preventScroll: true });
  }
}
const slugOf = (clip) => clip.share_url.split('/c/').pop();

function seconds(value) {
  const total = Math.round(value);
  if (total < 60) return `${total}s`;
  const m = Math.floor(total / 60);
  const s = String(total % 60).padStart(2, '0');
  return `${m}m ${s}s`;
}

function clock(value) {
  const total = Math.floor(value);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}

const relative = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });
function ago(iso) {
  const diff = (new Date(iso) - Date.now()) / 1000;
  const units = [['year', 31536000], ['month', 2592000], ['week', 604800], ['day', 86400],
    ['hour', 3600], ['minute', 60]];
  for (const [unit, size] of units) {
    if (Math.abs(diff) >= size) return relative.format(Math.round(diff / size), unit);
  }
  return 'just now';
}

function ordinal(n) {
  const suffix = n % 100 >= 11 && n % 100 <= 13 ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' }[n % 10] || 'th');
  return `${n}${suffix}`;
}

function clippedOn(iso) {
  const d = new Date(iso);
  const month = d.toLocaleString('en-US', { month: 'short' });
  const time = d.toLocaleString('en-US', { hour: 'numeric', minute: '2-digit' }).replace(' ', '');
  return `${month} ${ordinal(d.getDate())}, ${d.getFullYear()} at ${time}`;
}

function elapsed(iso) {
  return iso ? clock(Math.max(0, (Date.now() - new Date(iso)) / 1000)) : '';
}

// A running clock: its text is filled in (and kept current) by tickClocks, not baked into the page's HTML,
// so a poll only redraws the page when the run itself moved on.
const since = (iso) => (iso ? `<span data-since="${esc(iso)}"></span>` : '');
function tickClocks() {
  for (const el of document.querySelectorAll('[data-since]')) el.textContent = elapsed(el.dataset.since);
}
setInterval(tickClocks, 1000);

// --- navigation --------------------------------------------------------------

function navigate(path, { replace = false } = {}) {
  if (path !== location.pathname + location.search) history[replace ? 'replaceState' : 'pushState']({}, '', path);
  route();
}

document.addEventListener('click', (event) => {
  const link = event.target.closest('a[data-link]');
  if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
  event.preventDefault();
  navigate(link.getAttribute('href'));
});
window.addEventListener('popstate', route);

// YouTube storyboard frames can be missing; fall back to the video thumbnail.
document.addEventListener('error', (event) => {
  const img = event.target;
  if (img.tagName !== 'IMG') return;
  if (img.dataset.fallback && img.src !== img.dataset.fallback) img.src = img.dataset.fallback;
  else img.closest('.thumb-box')?.classList.add('failed'); // the sweep stops; the box stays dark
}, true);

// A .thumb-box sweeps softly until its thumbnail arrives, then fades it in. Load events don't bubble, so
// they're caught on the way down. One already loaded on this page (a redraw, the same video elsewhere)
// is marked before it's ever painted, so it shows at once instead of fading in again; a lazy image
// doesn't say it's complete until it's near the screen, so the addresses loaded so far are kept.
const loadedThumbs = new Set();
document.addEventListener('load', (event) => {
  const img = event.target;
  if (img.tagName !== 'IMG') return;
  loadedThumbs.add(img.src);
  img.closest('.thumb-box')?.classList.add('loaded');
}, true);
function settleThumbs(root) {
  for (const img of root.querySelectorAll('.thumb-box:not(.loaded) img')) {
    if ((img.complete && img.naturalWidth) || loadedThumbs.has(img.src)) img.closest('.thumb-box').classList.add('loaded');
  }
}

// Lists load their next page as it nears the bottom of the screen: their More button is pressed for
// them. It stays as the way to try again after a failure (moreFailed), and for anyone tabbing to it.
const MORE_AHEAD_PX = 900;
function pressNearbyMore() {
  for (const button of document.querySelectorAll('[data-auto-more]')) {
    if (button.disabled || button.dataset.failed || !button.offsetParent) continue;
    if (button.getBoundingClientRect().top < innerHeight + MORE_AHEAD_PX) button.click();
  }
}
let moreFrame = 0;
const checkMore = () => {
  if (!moreFrame) moreFrame = requestAnimationFrame(() => { moreFrame = 0; pressNearbyMore(); });
};
main.addEventListener('scroll', checkMore, { passive: true });
addEventListener('scroll', checkMore, { passive: true }); // the narrow layout scrolls the page, not #main
addEventListener('resize', checkMore);
new MutationObserver((records) => {
  for (const record of records) {
    for (const node of record.addedNodes) if (node.nodeType === 1) settleThumbs(node.parentElement || node);
  }
  checkMore(); // a short page leaves its More button in view
}).observe(document.body, { childList: true, subtree: true });

const VIEWS = { r: 'run', c: 'clip', settings: 'settings', search: 'search', saved: 'saved',
  subscriptions: 'subscriptions', episodes: 'episodes', labs: 'lab', compilations: 'compilations', tv: 'tv' };
const TITLES = { run: 'Run', clip: 'Clip', settings: 'Settings', search: 'Search', saved: 'Saved',
  subscriptions: 'Subscriptions', episodes: 'Episodes', compilations: 'Compilations', tv: 'TV' };
const setTitle = (title) => { document.title = title ? `${title} · Yolt` : 'Yolt'; };
let routedPath = null; // the page last routed to: moving to another one moves focus to it

async function route() {
  const [, kind, id] = location.pathname.split('/');
  if (kind === 'themes') { // Themes are a panel on the search page now: an old link opens it there
    const next = new URLSearchParams(location.search);
    next.set('themes', '1');
    history.replaceState({}, '', `/search?${next}`);
    return route();
  }
  const params = new URLSearchParams(location.search);
  const view = VIEWS[kind] || 'home';
  const current = { name: view, id };
  state.view = current;
  const path = location.pathname + location.search;
  const moved = routedPath !== null && path !== routedPath;
  routedPath = path;
  setTitle(TITLES[view]);
  renderSidebar();
  // The page opens with the nav as it is, not sliding or fading into it (index.html's `booting`): it's drawn
  // once without transitions, and only later changes have them.
  if (document.documentElement.classList.contains('booting')) {
    document.body.getBoundingClientRect();
    document.documentElement.classList.remove('booting');
  }
  try {
    if (view === 'home') await showHome();
    else if (view === 'run') await showRun(id);
    else if (view === 'clip') await showClip(id);
    else if (view === 'search') await showSearch(params);
    else if (view === 'subscriptions') await showSubscriptions(params);
    else if (view === 'episodes') await showEpisodes(params);
    else if (view === 'saved') await showSaved();
    else if (view === 'tv') await showTv(params);
    else if (view === 'lab') await labs.page(id, params, current);
    else if (view === 'compilations') await labs.compilations();
    else await showSettings();
  } catch (error) {
    if (state.view !== current) return; // a page already left failed
    main.innerHTML = `<div class="empty-state"><div><p>${esc(error.message)}</p>
      <a class="box-button small" href="/" data-link>Back home</a></div></div>`;
  }
  if (state.view !== current) return;
  main.scrollTop = 0;
  // A screen reader starts reading the new page, and Tab continues from it, not from the old link.
  if (moved && !main.contains(document.activeElement)) main.focus({ preventScroll: true });
}

// --- sidebar -----------------------------------------------------------------

// The queue, and the sidebar's totals with it. Cheap whatever its length: polled while runs are going.
// Returns whether it changed (a run queued, started, ended or cancelled).
async function loadQueue() {
  const before = state.queueLoaded ? queueKey() : null;
  state.queue = await api('/queue');
  state.queueLoaded = true;
  const changed = queueKey() !== before;
  renderSidebar();
  return changed;
}

// The nav: which tab the page is under, how many channels are subscribed to and how many episodes there are.
// Search, Saved and Compilations (its tools are labs) are tabs inside Episodes, so they're all under it: in the
// sidebar (Settings → Appearance) they're listed under Episodes while you're in it, the one you're on marked.
function renderSidebar() {
  const { name } = state.view || {};
  const current = { run: 'episodes', search: 'episodes', saved: 'episodes', settings: 'settings',
    subscriptions: 'subscriptions', episodes: 'episodes', tv: 'tv', lab: 'episodes', compilations: 'episodes' }[name];
  const sub = { saved: 'saved', search: 'search', compilations: 'compilations', lab: 'compilations' }[name];
  for (const link of document.querySelectorAll('[data-nav]')) {
    // Episodes is the page itself, or (aria-current="true") the section a sub-tab's page is in.
    if (link.dataset.nav === current) link.setAttribute('aria-current', sub ? 'true' : 'page');
    else link.removeAttribute('aria-current');
  }
  redraw($('#nav-sub-links'), episodeTabs(sub), 'href'); // only when they change: this runs on every poll
  const { subscriptions = 0, episodes = 0 } = state.queue.totals || {};
  $('#subs-count').textContent = subscriptions || '';
  $('#episodes-count').textContent = episodes || '';
  renderSavedCount();
  renderHomeLinks();
}

const tabLink = (id, current, label) =>
  `<a href="/${id}" data-link${id === current ? ' aria-current="page"' : ''}>${label}</a>`;

// Compilations (the labs' compilation tools) is hidden for now: no tab, though /compilations still opens it.
const SHOW_COMPILATIONS = false;

// The tabs inside Episodes: in its pages' heading (libraryTabs) and, in the sidebar, under it (renderSidebar).
const episodeTabs = (current) =>
  tabLink('saved', current, `Saved <span class="count saved-count">${saved.length || ''}</span>`)
  + (SHOW_COMPILATIONS ? labs.compilationsTab(current === 'compilations') : '') + tabLink('search', current, 'Search');

// The Episodes tab's own tabs, over each of its pages: Episodes, Saved, Compilations (shown while a compilation
// tool is switched on, and SHOW_COMPILATIONS) and Search, set large, as the page's heading. The page's own h1 stays for screen
// readers (app.css hides it under these).
function libraryTabs(current) {
  return `<nav class="library-tabs" aria-label="Episodes, saved clips, compilations and search">
    ${tabLink('episodes', current, 'Episodes')}${episodeTabs(current)}</nav>`;
}

// --- clip cards --------------------------------------------------------------

// Clips seen on any page, so saving one knows its share slug without another request.
const clipCache = new Map();

function usageBadge(usage) {
  if (!usage || !usage.revisions) return '';
  const label = usage.rendered ? `rendered ${usage.rendered}×`
    : usage.compilations ? `in ${usage.compilations} comp.` : 'used before';
  return `<span class="usage-badge" title="Used in ${usage.revisions} revision(s); ${usage.rendered} render(s)">${label}</span>`;
}

// Escaped text with the query's words marked. Search matches word stems ("creativity" finds
// "creative"), so each query word is marked by its stem-like prefix and whatever follows it.
// Matching runs on the raw text and each piece is escaped, so a mark can never split an entity.
function highlight(text, query) {
  const raw = String(text ?? '');
  const words = (query || '').toLowerCase().match(/[\p{L}\p{N}]{3,}/gu) || [];
  if (!words.length) return esc(raw);
  const stems = words.map((w) => w.slice(0, Math.max(3, w.length - 3)).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const pattern = new RegExp(`(?<![\\p{L}\\p{N}])(?:${stems.join('|')})[\\p{L}\\p{N}]*`, 'giu');
  let out = '';
  let last = 0;
  for (const match of raw.matchAll(pattern)) {
    out += `${esc(raw.slice(last, match.index))}<mark>${esc(match[0])}</mark>`;
    last = match.index + match[0].length;
  }
  return out + esc(raw.slice(last));
}

function clipCard(clip, { from = false, query = null } = {}) {
  clipCache.set(clip.id, clip);
  const passage = clip.match?.passages?.[0]?.text;
  const quote = query && passage ? `“${highlight(passage, query)}”`
    : clip.text ? `“${esc(clip.text)}”` : esc(clip.summary || '');
  const similarity = clip.match?.similarity != null ? `<span class="match">${Math.round(clip.match.similarity * 100)}% match</span>` : '';
  return `<div class="clip-cell">
    <a class="clip-card" href="/c/${esc(slugOf(clip))}" data-link>
      <div class="clip-head"><span class="title">${esc(clip.title)}</span>
        <span class="length">${seconds(clip.duration_ms / 1000)}</span></div>
      <span class="thumb-box"><img class="thumb" loading="lazy" alt="" src="${esc(clip.thumbnail_url)}"
        data-fallback="${esc(clip.links.video_thumbnail)}"></span>
      <p class="quote">${quote}</p>
      ${from && clip.episode ? `<p class="from">${esc(clip.episode.title || '')}</p>` : ''}
      <p class="card-meta">${similarity}${usageBadge(clip.usage)}</p>
    </a>
    ${saveButton(clip)}
  </div>`;
}

// --- home --------------------------------------------------------------------
// Quick links (the full TV, Episodes, Subscriptions) with the TV (tv.js compact) on their right; the newest of
// three lists (videos found on subscriptions, episodes clipped, clips); a row of For you (labs/foryou.js) and
// the best clips of the week; then the biggest themes. A queue change redraws the links, best and lists (renderSidebar,
// refreshHome), not the TV, so it keeps playing.

const HOME_ITEMS = 5;
const HOME_BEST = 6; // two rows of three cards
const HOME_THEMES = 6;
const HOME_THEMES_K = 12; // fewer, broader themes than Search's 20

async function showHome() {
  const { view } = state;
  const lists = await homeLists();
  if (!stillOn(view, 'home')) return;
  main.innerHTML = `<div class="home">
    <div class="home-top">
      <nav class="home-links" aria-label="Go to">
        <a class="home-link" href="/tv" data-link><strong>TV</strong><span>Your clips, back to back</span></a>
        <a class="home-link" href="/episodes" data-link><strong>Episodes</strong><span data-home="episodes"></span></a>
        <a class="home-link" href="/subscriptions" data-link><strong>Subscriptions</strong><span data-home="subs"></span></a>
      </nav>
      <div class="home-tv" id="home-tv"></div>
    </div>
    <div class="home-columns" id="home-columns"></div>
    <div class="home-picks">
      <div id="labs-home"></div>
      <section class="home-col" id="home-best"></section>
    </div>
    <section class="home-section" id="home-themes"></section>
  </div>`;
  renderHomeLinks();
  renderHomeLists(lists);
  startHomeTv(view, lists);
  loadHomeBest(view, lists.runs);
  loadHomeThemes(view);
  if (lists.clips.length) labs.home(view);
}

async function refreshHome(view) {
  const lists = await homeLists();
  if (!stillOn(view, 'home')) return;
  renderHomeLists(lists);
  startHomeTv(view, lists); // the first clips: the TV can start
  loadHomeBest(view, lists.runs);
}

async function homeLists() {
  const [videos, runs, clips] = await Promise.all([
    api(`/subscriptions/videos?limit=${HOME_ITEMS}`).then((page) => page.data).catch(() => null),
    api('/runs?status=ready&limit=50').then((page) => page.data).catch(() => null),
    api(`/clips?sort=-created&limit=${HOME_ITEMS}`).then((page) => page.data),
  ]);
  // An episode clipped more than once is listed once, by its newest run.
  const seen = new Set();
  const episodes = runs?.filter((run) => !seen.has(run.episode.id) && seen.add(run.episode.id)).slice(0, HOME_ITEMS);
  return { videos, runs, episodes, clips };
}

// Best of the week: the top-scoring clips of the runs that finished in the last 7 days (of the newest 50).
async function loadHomeBest(view, runs) {
  const since = Date.now() - 7 * 86_400_000;
  const week = (runs || []).filter((run) => run.finished_at && Date.parse(run.finished_at) > since);
  const query = new URLSearchParams([...week.map((run) => ['run_id', String(run.id)]), ['sort', '-score'],
    ['limit', String(HOME_BEST)]]);
  const clips = week.length ? await api(`/clips?${query}`).then((page) => page.data).catch(() => []) : [];
  const box = $('#home-best');
  if (!stillOn(view, 'home') || !box) return;
  redraw(box, clips.length ? `<h2 class="label"><span>Best of the week</span></h2>
    <div class="clip-grid recent home-best">${clips.map((c) => clipCard(c, { from: true })).join('')}</div>` : '');
}

// Themes: the biggest of a clustering of HOME_THEMES_K, each opening Search within it. The clustering is
// kept by the server until the vectors change, so this is a lookup after the first time.
async function loadHomeThemes(view) {
  const embedded = await api('/').then((info) => info.embeddings.vectors.clips.embedded).catch(() => 0);
  if (embedded < 2) return; // a clustering needs two clips
  const k = Math.min(HOME_THEMES_K, embedded);
  const data = await api(`/themes?k=${k}&sort=-clips`).catch(() => null);
  const box = $('#home-themes');
  if (!data || !stillOn(view, 'home') || !box) return;
  for (const t of data.data) themeLabels.set(t.id, themeLabel(t));
  box.innerHTML = `<h2 class="label"><span>Themes</span><a href="/search?themes=1&k=${k}&sort=-clips" data-link>All</a></h2>
    <div class="theme-grid home-themes">${data.data.slice(0, HOME_THEMES).map((t) => `<a class="theme-card"
      href="/search?theme=${esc(t.id)}" data-link>
      <p class="keywords">${t.keywords.map(esc).join(' · ') || '—'}</p>
      <ul>${t.representatives.slice(0, 2).map((r) => `<li>${esc(r.title)}</li>`).join('')}</ul>
      <p class="card-meta">${t.clip_count} clips · ${t.episode_count} episodes</p></a>`).join('')}</div>`;
}

function startHomeTv(view, { clips }) {
  const box = $('#home-tv');
  if (!box || box.dataset.state === 'tv' || (!clips.length && box.dataset.state === 'empty')) return;
  if (!clips.length) {
    box.dataset.state = 'empty';
    box.innerHTML = `<div class="empty-state"><div><p>Paste a YouTube link and get back its best moments.</p>
      <button type="button" class="box-button small" data-open-submit>+ Youtube URL</button></div></div>`;
    return;
  }
  box.dataset.state = 'tv';
  box.innerHTML = '';
  loadTv().then(({ show }) => {
    if (stillOn(view, 'home') && box.isConnected) return show(new URLSearchParams(), tvApp, { into: box, compact: true });
  }).catch((error) => {
    if (box.isConnected) box.innerHTML = `<p class="error-text">The TV didn't load. ${esc(error.message)}</p>`;
  });
}

// The quick links' second lines, from the queue's totals; renderSidebar keeps them current.
function renderHomeLinks() {
  const subs = $('[data-home="subs"]');
  if (!subs) return;
  const { subscriptions, episodes } = state.queue.totals || {};
  const { processing, queued } = state.queue;
  subs.textContent = subscriptions ? `${subscriptions} ${subscriptions === 1 ? 'channel' : 'channels'} followed`
    : 'Follow channels: their new videos turn up here';
  $('[data-home="episodes"]').textContent = processing.length
    ? `Clipping now${queued.length ? ` · ${queued.length} queued` : ''}`
    : queued.length ? `${queued.length} queued`
      : episodes ? `${episodes} ${episodes === 1 ? 'episode' : 'episodes'} clipped` : 'Every video you clip';
}

function renderHomeLists({ videos, episodes, clips }) {
  const column = (title, href, items, row, empty) => `<section class="home-col">
      <h2 class="label">${title}${href ? `<a href="${href}" data-link>All</a>` : ''}</h2>
      ${!items ? '<p class="meta-line">Couldn’t load these.</p>'
    : items.length ? `<ul class="home-list">${items.map(row).join('')}</ul>` : `<p class="meta-line">${empty}</p>`}
    </section>`;
  redraw($('#home-columns'),
    column('New from subscriptions', '/subscriptions', videos, homeVideo, 'No videos found yet. Follow a channel to see its new ones here.')
    + column('Recently clipped episodes', '/episodes', episodes, homeEpisode, 'Nothing clipped yet.')
    + column('Recent clips', null, clips, homeClip, 'No clips yet.'));
}

// One line of a home list. `open` is its link (or, for a found video not clipped yet, the button that opens
// the clip popup); it covers the row (CSS).
function homeRow({ thumb, fallback = '', open, meta, action = '' }) {
  return `<li class="home-item">
    <span class="thumb-box${thumb ? '' : ' failed'}">${thumb ? `<img class="thumb" loading="lazy" alt="" src="${esc(thumb)}"${
    fallback ? ` data-fallback="${esc(fallback)}"` : ''}>` : ''}</span>
    <div class="home-main">${open}<p class="home-meta">${meta.filter(Boolean).map(esc).join(' · ')}</p>${action}</div>
  </li>`;
}

function homeVideo(video) {
  const { run } = video;
  const title = `<span class="home-title">${esc(video.title)}</span>`;
  const status = !run ? '' : isActive(run) ? runStage(run)
    : run.status === 'ready' ? `✓ ${run.clip_count} ${run.clip_count === 1 ? 'clip' : 'clips'}` : `last run ${run.status}`;
  return homeRow({
    thumb: video.thumbnail_url,
    open: run ? `<a class="home-open" href="/r/${esc(run.id)}" data-link>${title}</a>`
      : `<button type="button" class="home-open" data-open-submit="${esc(video.url)}" title="Clip it">${title}</button>`,
    meta: [video.channel.name, video.published_at ? ago(video.published_at) : '', status],
  });
}

function homeEpisode(run) {
  const { episode } = run;
  return homeRow({
    thumb: videoThumb(episode.source),
    open: `<a class="home-open" href="/r/${esc(run.id)}" data-link><span class="home-title">${esc(episode.title)}</span></a>`,
    meta: [episode.podcast, `${run.clip_count} ${run.clip_count === 1 ? 'clip' : 'clips'}`,
      run.finished_at ? ago(run.finished_at) : ''],
    action: clipAgainButton(episode, 'link-button'),
  });
}

function homeClip(clip) {
  clipCache.set(clip.id, clip);
  return homeRow({
    thumb: clip.thumbnail_url,
    fallback: clip.links.video_thumbnail,
    open: `<a class="home-open" href="/c/${esc(slugOf(clip))}" data-link><span class="home-title">${esc(clip.title)}</span></a>`,
    meta: [clip.episode?.podcast, seconds(clip.duration_ms / 1000)],
  });
}

// --- run ---------------------------------------------------------------------

const PARAKEET_STAGES = [
  ['fetching', 'Fetching video info'],
  ['downloading', 'Downloading audio'],
  ['transcribing', 'Transcribing with Parakeet'],
  ['identifying', 'Finding highlights'],
];
const CAPTION_STAGES = [
  ['fetching', 'Fetching video info'],
  ['captions', 'Loading YouTube transcript'],
  ['identifying', 'Finding highlights'],
];

const usesCaptions = (run) => run.stage === 'captions'
  || run.transcript_source === 'youtube' || run.transcript_source === 'youtube_auto';

const TRANSCRIPT_SOURCES = {
  parakeet: 'Transcribed by Parakeet',
  youtube: 'YouTube captions',
  youtube_auto: 'YouTube auto-captions',
};

function stageInfo(run, stage) {
  if (run.stage !== stage) return '';
  if (stage === 'downloading' && run.progress != null) return `${Math.round(run.progress * 100)}%`;
  return since(run.stage_started_at);
}

// Highlightr's steps within "Finding highlights", then Yolt saving the clips. `waiting` and topic
// checks only show when they happen.
const HIGHLIGHT_STEPS = [
  ['waiting', 'Waiting for Highlightr (busy with other jobs)', true],
  ['discovering', 'Reading the transcript window by window'],
  ['verifying_topics', 'Checking topics', true],
  ['ranking', 'Ranking every candidate together'],
  ['writing_headlines', 'Writing headlines and descriptions'],
  ['saving', 'Saving clips'],
];

function highlightSteps(run) {
  const order = HIGHLIGHT_STEPS.map(([key]) => key);
  const current = run.status === 'failed' ? -1 : order.indexOf(run.step ?? 'discovering');
  const rows = HIGHLIGHT_STEPS.filter(([key, , optional], index) => !optional || index === current)
    .map(([key, label]) => {
      const index = order.indexOf(key);
      const cls = index < current ? 'done' : index === current ? 'active' : '';
      const mark = index < current ? '✓' : index === current ? '›' : '·';
      const info = index !== current ? ''
        : key === 'discovering' || key === 'saving' ? esc(run.detail || '') : since(run.step_started_at);
      return `<li class="${cls}"><span class="mark">${mark}</span>${label}<span class="info">${info}</span></li>`;
    }).join('');
  return `<ol class="steps substeps">${rows}</ol>`;
}

function progressBox(run) {
  if (run.status === 'failed') {
    return `<div class="status-box failed">
      <p class="error-text">${esc(run.error || 'Something went wrong.')}</p>
      <button type="button" class="box-button small" data-retry="${esc(run.id)}">Retry</button>
    </div>`;
  }
  if (run.status === 'cancelled') {
    return `<div class="status-box">
      <p class="meta-line">Cancelled. Retrying starts it again, reusing the transcript if it was finished.</p>
      <button type="button" class="box-button small" data-retry="${esc(run.id)}">Retry</button>
    </div>`;
  }
  const stages = usesCaptions(run) ? CAPTION_STAGES : PARAKEET_STAGES;
  const current = stages.findIndex(([key]) => key === run.stage);
  const steps = stages.map(([key, label], index) => {
    const cls = index < current ? 'done' : index === current ? 'active' : '';
    const mark = index < current ? '✓' : index === current ? '›' : '·';
    const nested = key === 'identifying' && index === current ? highlightSteps(run) : '';
    return `<li class="${cls}"><span class="mark">${mark}</span>${label}
      <span class="info">${stageInfo(run, key)}</span>${nested}</li>`;
  }).join('');
  const known = run.progress != null;
  return `<div class="status-box" aria-live="polite">
    ${run.status === 'queued' ? '<p class="meta-line">Queued — waiting for the current video to finish.</p>' : ''}
    <ol class="steps">${steps}</ol>
    <div class="bar ${known ? '' : 'indeterminate'}"><span style="${known ? `width:${run.progress * 100}%` : ''}"></span></div>
    <button type="button" class="box-button small cancel-run" data-cancel="${esc(run.id)}">Cancel</button>
  </div>`;
}

// Subscribe to the episode's channel, or say what's followed with a link to its found videos: a small pill
// after the channel's name. `full` is the episode from GET /episodes/{id}, which knows the subscription.
function channelSubscription(full) {
  if (!full?.podcast_id) return '';
  const sub = full.subscription;
  if (sub) {
    const what = FOLLOW_OPTIONS.find(([value]) => value === followOf(sub))[1].toLowerCase();
    return `<a class="sub-pill on" href="/subscriptions?channel=${esc(full.podcast_id)}" data-link
      title="Change what's followed or unsubscribe on the Subscriptions page">✓ Subscribed · ${esc(what)}</a>`;
  }
  if (!full.podcast_url) return '';
  return `<button type="button" class="sub-pill" data-subscribe-channel="${esc(full.podcast_url)}"
    title="Find this channel's new long uploads on the Subscriptions page">+ Subscribe</button>
    <span class="sub-error" role="alert"></span>`;
}

// An episode's length in words: "2 hr 4 min", "38 min".
function runtime(ms) {
  const minutes = Math.round(ms / 60000);
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return h ? `${h} hr${m ? ` ${m} min` : ''}` : `${Math.max(1, m)} min`;
}

function clipAgainButton(episode, buttonClass) {
  const url = httpsUrl(episode.source.url);
  return url ? `<button type="button" class="clip-again ${buttonClass}" data-open-submit="${esc(url)}"
    aria-label="Clip again: ${esc(episode.title)}">Clip again</button>` : '';
}

// The episode's card at the top of its run's page: its thumbnail (to the video), channel, title, facts and
// links, then a list of the rest (who's in it, what the run looked for, its transcript, when it was clipped).
// `full` is the episode from GET /episodes/{id} (subscription, speakers, transcripts), or null if that failed.
function runHeader(run, full = null) {
  const { episode } = run;
  const ready = run.status === 'ready';
  const youtube = httpsUrl(episode.source.url) ? episode.source.url : '';
  const thumb = videoThumb(episode.source, 'hqdefault');
  const picture = thumb ? `<img class="thumb" alt="" src="${esc(thumb)}">` : '';
  // The thumbnail is a second way to the video, for the mouse: the button below is the one Tab and screen readers get.
  const cover = youtube
    ? `<a class="thumb-box episode-thumb${thumb ? '' : ' failed'}" href="${esc(youtube)}" target="_blank" rel="noopener"
        tabindex="-1" aria-hidden="true">${picture}<span class="play"></span></a>`
    : `<span class="thumb-box episode-thumb${thumb ? '' : ' failed'}">${picture}</span>`;
  const channel = episode.podcast
    ? `<p class="channel">${httpsUrl(episode.podcast_url)
      ? `<a href="${esc(episode.podcast_url)}" target="_blank" rel="noopener">${esc(episode.podcast)}</a>`
      : `<span>${esc(episode.podcast)}</span>`}${channelSubscription(full)}</p>`
    : '';
  const facts = [];
  if (episode.published_at) facts.push(`Published ${ago(episode.published_at)}`);
  if (episode.duration_ms) facts.push(runtime(episode.duration_ms));
  if (ready) facts.push(`${run.clip_count} ${run.clip_count === 1 ? 'clip' : 'clips'}`);
  const links = [];
  if (youtube) links.push(`<a class="box-button small" href="${esc(youtube)}" target="_blank" rel="noopener">Watch on YouTube ↗</a>`);
  const reclip = clipAgainButton(episode, 'box-button small');
  if (reclip) links.push(reclip);
  const details = [];
  if (full) details.push(`<dt>Featuring</dt><dd id="speakers-line">${speakersList(full)}</dd>`);
  if (run.instructions) details.push(`<dt>Focus</dt><dd>${esc(run.instructions)}</dd>`);
  if (ready && run.transcript_id) {
    const words = full?.transcripts?.find((t) => t.id === run.transcript_id)?.word_count;
    const about = [TRANSCRIPT_SOURCES[run.transcript_source], words ? `${words.toLocaleString('en-US')} words` : '']
      .filter(Boolean).join(' · ');
    if (about) details.push(`<dt>Transcript</dt><dd>${esc(about)}</dd>`);
  }
  if (ready && run.finished_at) details.push(`<dt>Clipped</dt><dd>${esc(clippedOn(run.finished_at))}</dd>`);
  return `<header class="episode-card">
    ${cover}
    <div class="episode-main">
      ${channel}
      <h1>${esc(episode.title)}</h1>
      ${facts.length ? `<p class="episode-facts">${facts.map(esc).join(' · ')}</p>` : ''}
      ${links.length ? `<div class="actions">${links.join('')}</div>` : ''}
    </div>
    ${details.length ? `<dl class="facts">${details.join('')}</dl>` : ''}
  </header>`;
}

// Who's in the episode, for its Featuring row (#speakers-line), with a button to correct it.
function speakersList(episode) {
  const people = episode.speakers || [];
  const names = people.map((s) => `${esc(s.name)}${s.role === 'host' ? ' <span class="role">host</span>' : ''}`).join(', ');
  const guessed = people.some((s) => !s.verified) ? ' <span class="guess" title="Inferred from the title, description and opening minutes; not confirmed">unconfirmed</span>' : '';
  const edit = ` <button type="button" class="link-button" data-edit-speakers="${esc(episode.id)}">${people.length ? 'Edit' : 'Add'}</button>`;
  return `${people.length ? names + guessed : '<span class="muted">nobody identified yet</span>'}${edit}`;
}

// A line saying some clips can't be found by meaning yet, and why; '' when they all can.
// `info` is GET / (its `embeddings`); `pending` and `total` count the clips in question.
function embeddingNotice(info, pending, total = null) {
  if (!info || !pending) return '';
  const { embeddings } = info;
  if (embeddings.query_embedding === 'not_configured') {
    return '<p class="embed-notice">Search by meaning is off (no embeddings server), so clips are found by their words only.</p>';
  }
  const what = total != null ? `${pending} of ${total} clips` : `${pending} ${pending === 1 ? 'clip' : 'clips'}`;
  if (embeddings.last_error) {
    return `<p class="embed-notice bad">${what} not searchable by meaning yet: ${esc(embeddings.last_error.message)}
      Yolt retries every few minutes.</p>`;
  }
  return `<p class="embed-notice">${what} not searchable by meaning yet — embedding them now.</p>`;
}

async function showRun(id, { quiet = false } = {}) {
  const { view } = state;
  const run = await api(`/runs/${encodeURIComponent(id)}?include=clips`);
  const episode = await api(`/episodes/${run.episode.id}`).catch(() => null);
  const unembedded = run.status === 'ready' ? run.clip_count - run.embedded_clip_count : 0;
  const info = unembedded ? await api('/').catch(() => null) : null;
  if (!stillOn(view, 'run') || view.id !== id) return;
  view.data = run;
  setTitle(run.episode.title);
  // Keep polling until they're searchable, unless there's no embeddings server to make them so; and
  // only now and then while it's failing (Yolt itself retries every few minutes).
  view.embedPending = unembedded > 0 && info?.embeddings.query_embedding !== 'not_configured';
  view.embedNextAt = Date.now() + (info?.embeddings.last_error ? EMBED_RETRY_MS : 0);
  let body = '';
  if (run.status !== 'ready') body = progressBox(run);
  else if (!run.clips.length) body = '<p class="meta-line">Highlightr found no clips in this video.</p>';
  else body = `<div class="clip-grid">${run.clips.map((c) => clipCard(c)).join('')}</div>`;
  const html = runHeader(run, episode) + embeddingNotice(info, unembedded, run.clip_count) + body;
  if (!quiet || main.dataset.html !== html) {
    // A redraw leaves alone what the user is in the middle of: a speakers editor opened on this page
    // (redrawn around, never detached, so what's typed and focus stay put; Save and Cancel leave it by
    // routing), buttons still waiting on their request, and which control has focus.
    const editor = main.querySelector('#speakers-form');
    const keepEditor = editor && editorView === view;
    const waiting = [...main.querySelectorAll(BUSY_BUTTONS)].filter((b) => b.disabled);
    const focused = !keepEditor || !editor.contains(document.activeElement) ? focusKey(document.activeElement) : null;
    if (!keepEditor) main.innerHTML = html;
    else if (!redrawAround(editor.closest('#speakers-line'), html, '#speakers-line')) {
      if (quiet) return; // no Featuring row to keep it in (the episode didn't load): a refresh waits
      main.innerHTML = html;
    }
    for (const button of waiting) main.querySelector(focusKey(button))?.replaceWith(button);
    if (focused) main.querySelector(focused)?.focus({ preventScroll: true });
    main.dataset.html = html;
    tickClocks();
  }
}

// Redraws main as `html` while `kept`, an element in it, stays where it is (never detached): at each level
// from it up to main, what's beside it is swapped for what's beside `selector` in the new markup, and the
// elements it's in take their new attributes. False, and nothing changed, if the new markup has no
// `selector` at the same depth.
function redrawAround(kept, html, selector) {
  const fresh = document.createElement('template');
  fresh.innerHTML = html;
  const levels = [];
  let old = kept;
  let now = fresh.content.querySelector(selector);
  while (old && now && old !== main) {
    levels.push([old, now]);
    old = old.parentNode;
    now = now.parentNode;
  }
  if (old !== main || now !== fresh.content) return false;
  for (const [mine, theirs] of levels) {
    while (mine.previousSibling) mine.previousSibling.remove();
    while (mine.nextSibling) mine.nextSibling.remove();
    const before = [];
    for (let node = theirs.previousSibling; node; node = node.previousSibling) before.unshift(node);
    const after = [];
    for (let node = theirs.nextSibling; node; node = node.nextSibling) after.push(node);
    mine.before(...before);
    mine.after(...after);
    if (mine === kept) continue;
    for (const { name } of [...mine.attributes]) if (!theirs.hasAttribute(name)) mine.removeAttribute(name);
    for (const { name, value } of theirs.attributes) if (mine.getAttribute(name) !== value) mine.setAttribute(name, value);
  }
  return true;
}

// Buttons a redraw keeps as they are while their request is going (disabled), so they can't be clicked twice.
const BUSY_BUTTONS = '[data-subscribe-channel], [data-cancel], [data-retry]';
const KEY_ATTRIBUTES = ['data-subscribe-channel', 'data-cancel', 'data-retry', 'data-edit-speakers', 'data-save-clip',
  'data-youtube', 'data-open-submit'];

// A selector for the same control in a redrawn page, or null if there's no telling which it was.
function focusKey(el) {
  if (!el || !main.contains(el) || el === main) return null;
  if (el.id) return `#${CSS.escape(el.id)}`;
  const attribute = KEY_ATTRIBUTES.find((name) => el.hasAttribute(name));
  if (attribute) return `[${attribute}="${CSS.escape(el.getAttribute(attribute))}"]`;
  if (el.matches('a[href]')) return `a[href="${CSS.escape(el.getAttribute('href'))}"]`;
  return null;
}

main.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-subscribe-channel]');
  if (!button) return;
  button.disabled = true;
  button.textContent = 'Subscribing…';
  const { ok, data } = await apiRaw('/subscriptions', {
    method: 'POST', body: JSON.stringify({ url: button.dataset.subscribeChannel, ...followBody('uploads') }),
  }).catch((failure) => ({ ok: false, data: { detail: failure.message } }));
  if (!ok) {
    button.disabled = false;
    button.textContent = 'Subscribe';
    // Looked up now: a quiet refresh may have redrawn the line around the button meanwhile.
    const error = button.parentElement?.querySelector('.sub-error');
    if (error) error.textContent = problemText(data) || 'Couldn’t subscribe.';
    return;
  }
  loadQueue().catch(() => {}); // the sidebar's count
  if (state.view?.name === 'run') await showRun(state.view.id);
});

// --- clip --------------------------------------------------------------------

async function showClip(slug) {
  const { view } = state;
  const clip = await api(`/shares/${encodeURIComponent(slug)}?include=details,speakers`);
  clipCache.set(clip.id, clip);
  if (!stillOn(view, 'clip') || view.id !== slug) return;
  view.data = clip;
  setTitle(clip.title);
  const { episode } = clip;
  const siblings = await api(`/clips?run_id=${clip.run_id}&sort=rank&view=compact&limit=200`)
    .then((page) => page.data).catch(() => []);
  if (state.view !== view) return;
  const index = siblings.findIndex((c) => c.id === clip.id);
  const prev = siblings[index - 1];
  const next = siblings[index + 1];
  const score = clip.score != null ? `${Math.round(clip.score * 100)} / 100` : '—';
  const category = clip.details?.category?.primary;

  main.innerHTML = `
    <a class="crumb" href="/r/${esc(clip.run_id)}" data-link>← ${esc(episode.title)}</a>
    <article class="clip-detail">
      <div class="clip-detail-head">
        <div><h2>${esc(clip.title)}</h2><p class="length">${seconds(clip.duration_ms / 1000)}</p></div>
        ${saveButton(clip, { page: true })}
      </div>
      ${playerMarkup(clip)}
      ${clip.text ? `<p class="quote">“${esc(clip.text)}”</p>` : ''}
      <dl class="facts">
        ${clip.summary ? `<dt>About</dt><dd>${esc(clip.summary)}</dd>` : ''}
        <dt>Span</dt><dd>${clock(clip.time.start_ms / 1000)} – ${clock(clip.time.end_ms / 1000)} of
          <a href="/r/${esc(clip.run_id)}" data-link>${esc(episode.title)}</a></dd>
        <dt>Rank</dt><dd>#${clip.rank} · score ${score}${category ? ` · ${esc(category)}` : ''}</dd>
        ${clip.speakers?.length ? `<dt>Featuring</dt><dd>${clip.speakers.map((s) => esc(s.name)).join(', ')}${
    clip.speakers.some((s) => !s.verified) ? ' <span class="guess">unconfirmed</span>' : ''}</dd>` : ''}
        <dt>Used</dt><dd id="clip-usage">${clip.usage.revisions
    ? `in ${clip.usage.revisions} revision(s) of ${clip.usage.compilations} current compilation(s), rendered ${clip.usage.rendered}×`
    : 'not in any compilation yet'}</dd>
      </dl>
      <div class="actions">
        <a class="box-button small" href="/search?like=${esc(clip.id)}" data-link>More like this</a>
        ${labs.clipActions(clip)}
        <button type="button" class="box-button small" id="copy-link">Copy share link</button>
        <span class="note" id="copy-note" role="status"></span>
      </div>
      <div id="clip-appearances"></div>
      ${clip.text ? `<details class="transcript"><summary>Transcript</summary><p>${esc(clip.text)}</p></details>` : ''}
      <div id="labs-clip"></div>
    </article>
    <nav class="clip-nav">
      <span>${prev ? `<a href="/c/${esc(slugOf(prev))}" data-link>← ${esc(prev.title)}</a>` : ''}</span>
      <span>${next ? `<a href="/c/${esc(slugOf(next))}" data-link>${esc(next.title)} →</a>` : ''}</span>
    </nav>`;

  $('#copy-link').addEventListener('click', async () => {
    const note = $('#copy-note');
    try {
      await navigator.clipboard.writeText(clip.share_url);
      note.textContent = 'Link copied';
    } catch {
      note.textContent = clip.share_url;
    }
  });
  const box = $('#clip-appearances'); // this clip's, not whichever page is shown when the answer comes
  api(`/clips/${clip.id}/appearances`).then(({ data }) => {
    if (state.view !== view || !box.isConnected || !data.length) return;
    box.innerHTML = `<p class="meta-line appearances">In: ${data.map((a) => `${
      esc(a.title)} (rev ${a.number}, item ${a.position})`).join(', ')}</p>`;
  }).catch(() => {});
  labs.clip(clip, view);
  await mountPlayer(clip);
}

// --- player ------------------------------------------------------------------
// Like RipeClips: YouTube's own controls are hidden and our bar only spans the
// clip, so playback can't wander outside start–end. The IFrame API also reports
// when a video refuses to play embedded (owner or label restrictions), so we
// can offer YouTube instead of a dead player.

const CLIP_END_SLACK = 0.12; // the 150ms tick can't land exactly on the end
const LINE_CHARS = 42; // a subtitle line at most, as broadcast subtitles have it
const LINE_PAUSE_MS = 700; // a gap this long between words ends a line, as a sentence's end does

const clipStart = (clip) => clip.time.start_ms / 1000;
const clipEnd = (clip) => clip.time.end_ms / 1000;

// A clip's words as subtitles, the way pre-recorded video has them: blocks of one or two lines that appear and
// go together, so the words coming can be read ahead. A line is up to LINE_CHARS, ending early at a sentence's
// end or a pause once it's under way; a block ends with its second line or with a sentence. Returns the blocks
// as lists of lines, each a list of word indexes. The font is sized by the picture's width, so lines fit at any
// size.
function subtitleBlocks(words) {
  const blocks = [];
  let block = [];
  let line = [];
  let chars = 0;
  const endLine = (sentence) => {
    if (!line.length) return;
    block.push(line);
    line = [];
    chars = 0;
    if (block.length === 2 || sentence) {
      blocks.push(block);
      block = [];
    }
  };
  words.forEach((word, i) => {
    const length = word.word.length;
    if (line.length && chars + 1 + length > LINE_CHARS) endLine(false);
    line.push(i);
    chars += (chars ? 1 : 0) + length;
    const next = words[i + 1];
    const ends = /[.?!…][”’"')\]]*$/u.test(word.word) || (next && next.start_ms - word.end_ms >= LINE_PAUSE_MS);
    if (ends && chars >= LINE_CHARS * 0.4) endLine(true);
  });
  endLine(true);
  if (block.length) blocks.push(block);
  return blocks;
}

// Subtitles drawn into `box` (a .subs over a player's picture, app.css): show(words, ms) puts up the block being
// said `ms` into the video, whole, with the words still to come in it dimmed. Nothing shows before the first
// word, and a block stays up through a pause until the next one starts. clear() takes them down.
function subtitles(box) {
  let words = null;
  let blocks = [];
  let blockOf = []; // each word's block
  let at = -1; // the word being said, or -1 before the first
  let shown = -1; // the block shown, or -1 for none
  const clear = () => {
    words = null;
    at = -1;
    shown = -1;
    box.innerHTML = '';
  };
  const show = (list, ms) => {
    if (list !== words) { // another clip's words
      clear();
      words = list;
      blocks = subtitleBlocks(list);
      blockOf = blocks.flatMap((block, b) => block.flat().map(() => b));
    }
    let now = at >= 0 && words[at]?.start_ms > ms ? -1 : at; // went back: look from the start
    while (now + 1 < words.length && words[now + 1].start_ms <= ms) now += 1;
    if (now === at) return;
    at = now;
    const block = at < 0 ? -1 : blockOf[at];
    if (block !== shown) {
      shown = block;
      box.innerHTML = block < 0 ? '' : blocks[block].map((line) => `<p><span class="line">${
        line.map((i) => `<span data-i="${i}">${esc(words[i].word)}</span>`).join(' ')}</span></p>`).join('');
    }
    for (const span of box.querySelectorAll('[data-i]')) span.classList.toggle('ahead', Number(span.dataset.i) > at);
  };
  return { show, clear };
}

function playerMarkup(clip) {
  const length = clipEnd(clip) - clipStart(clip);
  return `<div class="player-stage" data-status="idle">
    <div class="player">
      <div id="player"></div>
      <img class="poster" alt="" src="${esc(clip.thumbnail_url)}" data-fallback="${esc(clip.links.video_thumbnail)}">
      <span class="play-badge" aria-hidden="true">▶</span>
      <button type="button" class="player-hit" data-play aria-label="Play clip" disabled></button>
      <div class="subs" aria-hidden="true"></div>
    </div>
    <div class="player-controls">
      <button type="button" class="ctl primary" data-play aria-label="Play clip" disabled>▶</button>
      <span class="time" data-elapsed>0:00</span>
      <input class="progress" data-progress type="range" min="0" max="100" step="0.1" value="0"
        aria-label="Clip position" disabled>
      <span class="time total">${clock(length)}</span>
      <button type="button" class="ctl" data-mute aria-pressed="false" disabled>VOL</button>
      <button type="button" class="ctl" data-cc aria-pressed="false" title="Subtitles" disabled>CC</button>
      <button type="button" class="ctl" data-fullscreen aria-label="Fullscreen" title="Fullscreen"
        ${document.fullscreenEnabled ? '' : 'hidden'}>⛶</button>
      <a class="ctl yt" data-youtube href="${esc(clip.links.watch)}" target="_blank" rel="noopener"
        title="Watch the full video on YouTube from ${clock(clipStart(clip))}">YouTube ↗</a>
    </div>
  </div>`;
}

// Whether a clip's subtitles show (drawn from its transcript over the picture, .subs in app.css). On unless
// turned off.
const CAPTIONS_KEY = 'yolt.captions.v1';
const captionsWanted = () => localStorage.getItem(CAPTIONS_KEY) !== 'off';

let youtubeApi;
function loadYouTubeApi() {
  youtubeApi ??= new Promise((resolve) => {
    window.onYouTubeIframeAPIReady = () => resolve(window.YT);
    const script = document.createElement('script');
    script.src = 'https://www.youtube.com/iframe_api';
    script.onerror = () => resolve(null);
    document.head.append(script);
  });
  return youtubeApi;
}

function playerFallback(box, clip) {
  box.innerHTML = `<a class="player-fallback" href="${esc(clip.links.watch)}" target="_blank" rel="noopener">
    <img alt="" src="${esc(clip.thumbnail_url)}" data-fallback="${esc(clip.links.video_thumbnail)}">
    <span>This video can't play here. Watch on YouTube from ${clock(clipStart(clip))} →</span></a>`;
}

// `autoplay` starts it once it's ready; `onComplete` runs each time it plays to the clip's end (labs' Thread
// plays the next clip then).
async function mountPlayer(clip, { autoplay = false, onComplete = null } = {}) {
  const stage = $('.player-stage');
  const box = $('.player', stage);
  const start = clipStart(clip);
  const end = clipEnd(clip);
  const length = end - start;
  const YT = await loadYouTubeApi();
  if (!box.isConnected) return;
  if (!YT) {
    // No IFrame API: fall back to YouTube's own controls, still limited to the span.
    stage.classList.add('native');
    box.innerHTML = `<iframe src="${esc(clip.links.embed)}" title="${esc(clip.title)}"
      allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowfullscreen></iframe>`;
    return;
  }

  const plays = stage.querySelectorAll('[data-play]'); // the video itself and the bar's button
  const playButton = $('.ctl.primary', stage);
  const progress = $('[data-progress]', stage);
  const elapsed = $('[data-elapsed]', stage);
  const muteButton = $('[data-mute]', stage);
  const ccButton = $('[data-cc]', stage);
  let ready = false;
  let status = 'idle'; // idle | playing | paused | complete
  let scrubbing = false;
  let muted = false;
  let captions = captionsWanted();
  const showCaptions = () => {
    stage.classList.toggle('subs-off', !captions);
    ccButton.setAttribute('aria-pressed', String(captions));
  };
  showCaptions();
  // The subtitles are the clip's words from its transcript (as on TV), asked for alongside the player.
  const subs = subtitles($('.subs', stage));
  let words = [];
  const showCc = () => {
    ccButton.disabled = !ready || !words.length;
    ccButton.title = words.length ? 'Subtitles' : 'No transcript to show as subtitles';
  };
  if (clip.words && clip.transcript_id != null) {
    apiRaw(`/transcripts/${clip.transcript_id}/words?first=${clip.words.first}&last=${clip.words.last}`)
      .then(({ ok, data }) => {
        words = ok ? data.words : [];
        showCc();
      }).catch(() => {});
  }

  const show = (time) => {
    const into = Math.min(length, Math.max(0, time - start));
    const percent = length > 0 ? (into / length) * 100 : 0;
    elapsed.textContent = clock(into);
    if (!scrubbing) progress.value = String(percent);
    progress.style.setProperty('--progress', `${percent}%`);
    subs.show(words, time * 1000);
  };
  const setStatus = (next) => {
    status = next;
    stage.dataset.status = next;
    const label = next === 'playing' ? 'Pause clip' : next === 'complete' ? 'Replay clip' : 'Play clip';
    plays.forEach((button) => button.setAttribute('aria-label', label));
    playButton.textContent = next === 'playing' ? '❚❚' : next === 'complete' ? '↺' : '▶';
  };
  const showMute = () => {
    muteButton.textContent = muted ? 'MUTED' : 'VOL';
    muteButton.setAttribute('aria-pressed', String(muted));
  };

  const player = new YT.Player('player', {
    // youtube.com, not youtube-nocookie.com, so the player uses the browser's YouTube login (Premium, age checks).
    host: 'https://www.youtube.com',
    videoId: clip.source.video_id,
    playerVars: {
      start: Math.floor(start),
      controls: 0,
      disablekb: 1,
      fs: 0,
      iv_load_policy: 3,
      rel: 0,
      playsinline: 1,
      origin: window.location.origin,
    },
    events: {
      onReady: () => {
        ready = true;
        muted = player.isMuted();
        showMute();
        [...plays, progress, muteButton].forEach((control) => { control.disabled = false; });
        showCc();
        if (autoplay) player.playVideo();
      },
      onStateChange: ({ data }) => {
        if (data === YT.PlayerState.PLAYING) {
          // YouTube's captions would say it twice (and fall on the edge cropped off). Not in the IFrame API's
          // documentation, but what YouTube's own CC button does; TV does the same.
          if (!stage.classList.contains('started')) player.unloadModule?.('captions');
          stage.classList.add('started');
          if (status !== 'complete') setStatus('playing');
        } else if (data === YT.PlayerState.PAUSED && status === 'playing') {
          setStatus('paused');
        } else if (data === YT.PlayerState.ENDED) {
          const was = status;
          show(end);
          setStatus('complete');
          if (was !== 'complete') onComplete?.();
        }
      },
      onError: () => {
        ready = false;
        stage.classList.add('failed');
        playerFallback(box, clip);
      },
    },
  });

  // Keeps playback inside start–end and drives the bar. Stops on its own once
  // the clip page is replaced.
  const timer = setInterval(() => {
    if (!stage.isConnected) {
      clearInterval(timer);
      player.destroy?.();
      return;
    }
    if (!ready || status !== 'playing') return;
    const time = Number(player.getCurrentTime?.());
    if (!Number.isFinite(time)) return;
    if (time >= end - CLIP_END_SLACK) {
      setStatus('complete');
      player.pauseVideo();
      show(end);
      onComplete?.();
    } else if (time < start - 0.5) {
      player.seekTo(start, true);
      show(start);
    } else {
      show(time);
    }
  }, 150);

  const toggle = () => {
    if (!ready) return;
    if (status === 'playing') {
      player.pauseVideo();
      setStatus('paused');
      return;
    }
    if (status === 'complete') {
      player.seekTo(start, true);
      show(start);
      setStatus('paused');
    }
    player.playVideo();
  };
  plays.forEach((button) => button.addEventListener('click', toggle));

  // While dragging, the tick mustn't pull the handle back to the player's time.
  progress.addEventListener('pointerdown', () => {
    scrubbing = true;
    window.addEventListener('pointerup', () => { scrubbing = false; }, { once: true });
  });
  // Dragging seeks without asking the server for more video (allowSeekAhead false); letting go does.
  const seek = (allowSeekAhead) => {
    if (!ready) return;
    const time = start + (Number(progress.value) / 100) * length;
    player.seekTo(time, allowSeekAhead);
    show(time);
    if (status === 'complete') setStatus('paused');
  };
  progress.addEventListener('input', () => seek(false));
  progress.addEventListener('change', () => seek(true));

  muteButton.addEventListener('click', () => {
    muted = !muted;
    if (muted) player.mute();
    else player.unMute();
    showMute();
  });

  ccButton.addEventListener('click', () => {
    captions = !captions;
    localStorage.setItem(CAPTIONS_KEY, captions ? 'on' : 'off');
    showCaptions();
  });

  $('[data-fullscreen]', stage).addEventListener('click', () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else stage.requestFullscreen?.();
  });

  // Leaving for the full video shouldn't leave the clip playing behind it.
  $('[data-youtube]', stage).addEventListener('click', () => {
    if (ready && status === 'playing') toggle();
  });
}

// --- settings ----------------------------------------------------------------

const FIELDS = {
  parakeet_url: 'Transcription service URL',
  parakeet_model: 'Transcription model',
  youtube_captions: 'Use YouTube\'s transcript when the video has one',
  highlightr_url: 'Highlightr service URL',
  llm_endpoint: 'LLM endpoint',
  llm_model: 'LLM model',
  llm_api_key: 'LLM API key',
  embed_url: 'Embeddings URL',
  embed_model: 'Embedding model',
  min_clip_seconds: 'Min length (s)',
  max_clip_seconds: 'Max length (s)',
  max_clips: 'Max clips',
  candidates_per_window: 'Candidates per window',
  clip_instructions: 'Default focus for new links',
  subscription_check_hours: 'Check every (hours)',
  subscription_min_minutes: 'Shortest video kept (min)',
  // Not in Settings for now (labs/index.js settings() draws its section): set by PUT /settings or YOLT_LABS_DISABLED.
  labs_disabled: 'Labs switched off',
  feed_mix: 'Mix by clip age',
};

// What a field shows: empty when it's at its default, which is the placeholder (an empty field saves as
// "use the default"). A field set by an environment variable shows that value, locked.
function shown(key, config) {
  const value = config.values[key];
  return config.env_overrides.includes(key) || value !== config.defaults[key] ? value : '';
}

function field(key, config, { type = 'text', test = null, placeholder = '' } = {}) {
  const locked = config.env_overrides.includes(key);
  const secret = key === 'llm_api_key';
  const value = secret ? '' : shown(key, config);
  const hint = secret && config.secrets_configured.includes(key) ? 'Saved — enter a new key to replace'
    : (!secret && config.defaults[key]) || placeholder;
  const input = `<input id="f-${key}" name="${key}" type="${type}" value="${esc(value)}" spellcheck="false"
    autocomplete="off" placeholder="${esc(hint)}" ${locked ? 'disabled' : ''}>`;
  return `<div class="field">
    <span class="label-row"><label for="f-${key}">${FIELDS[key]}</label>${locked ? '<span class="lock">Environment override</span>' : ''}
      ${test ? `<span class="result" id="r-${test}" role="status"></span>` : ''}</span>
    ${test ? `<span class="row">${input}<button type="button" class="box-button small" data-test="${test}"
      aria-label="Test ${esc(FIELDS[key])}">Test</button></span>` : input}
  </div>`;
}

const CAPTION_OPTIONS = [
  ['any', 'Yes — creator or auto-generated captions'],
  ['manual', 'Only creator-uploaded captions (punctuated text)'],
  ['off', 'No — always transcribe with Parakeet'],
];

function selectField(key, config, options) {
  const locked = config.env_overrides.includes(key);
  const value = config.values[key];
  return `<label class="field" for="f-${key}">
    <span class="label-row"><span>${FIELDS[key]}</span>${locked ? '<span class="lock">Environment override</span>' : ''}</span>
    <select id="f-${key}" name="${key}" ${locked ? 'disabled' : ''}>${options.map(([v, label]) =>
    `<option value="${v}" ${v === value ? 'selected' : ''}>${esc(label)}</option>`).join('')}</select>
  </label>`;
}

// Where the nav goes: across the top (the design's), or down the left side. A choice for this
// browser, not a server setting, so it changes the moment it's picked; index.html applies it before the first paint.
const NAV_KEY = 'yolt.nav.v1';
const NAV_OPTIONS = [['top', 'Across the top'], ['side', 'Down the left side']];
const navLayout = () => (localStorage.getItem(NAV_KEY) === 'side' ? 'side' : 'top');
function setNavLayout(layout) {
  localStorage.setItem(NAV_KEY, layout);
  document.documentElement.classList.toggle('nav-side', layout === 'side');
}

// Light or dark (html.dark, app.css :root.dark), or whichever the system is in, following it as it changes.
// Like the nav's place, a choice for this browser that index.html applies before the first paint; light by
// default, the design's own.
const MODE_KEY = 'yolt.mode.v1';
const MODE_OPTIONS = [['light', 'Light'], ['dark', 'Dark'], ['system', 'Match the system']];
const systemDark = matchMedia('(prefers-color-scheme: dark)');
const colorMode = () => {
  const mode = localStorage.getItem(MODE_KEY);
  return mode === 'dark' || mode === 'system' ? mode : 'light';
};
function applyColorMode() {
  const mode = colorMode();
  const dark = mode === 'dark' || (mode === 'system' && systemDark.matches);
  const root = document.documentElement;
  if (root.classList.contains('dark') === dark) return;
  // All at once, not each thing on its own transition (app.css .mode-switch).
  root.classList.add('mode-switch');
  root.classList.toggle('dark', dark);
  $('meta[name="theme-color"]')?.setAttribute('content', dark ? '#111113' : '#f5f5f7');
  document.body.getBoundingClientRect();
  root.classList.remove('mode-switch');
}
function setColorMode(mode) {
  localStorage.setItem(MODE_KEY, mode);
  applyColorMode();
}
systemDark.addEventListener('change', applyColorMode);
// Changed in another tab.
window.addEventListener('storage', (event) => { if (event.key === MODE_KEY) applyColorMode(); });

// Settings → Clear all: delete the whole catalogue once the word is typed, and forget this browser's saved clips.
async function clearAll(event) {
  const button = event.currentTarget;
  const status = $('#clear-status');
  const typed = prompt('This deletes every episode, clip, transcript, compilation and thumbnail. '
    + 'Subscriptions and settings stay. It can\'t be undone.\n\nType "clear" to confirm.');
  if (typed === null) return;
  if (typed.trim().toLowerCase() !== 'clear') {
    status.textContent = 'Nothing was cleared: type "clear" to confirm.';
    return;
  }
  button.disabled = true;
  status.textContent = 'Clearing…';
  const { ok, data } = await apiRaw('/catalogue/clear', {
    method: 'POST', body: JSON.stringify({ confirm: 'clear' }),
  }).catch((failure) => ({ ok: false, data: { detail: failure.message } }));
  button.disabled = false;
  if (!ok) {
    status.textContent = problemText(data) || 'Couldn’t clear the catalogue.';
    return;
  }
  storeSaved([]); // their clips are gone
  const n = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;
  status.textContent = `Cleared ${n(data.clips, 'clip')} from ${n(data.episodes, 'episode')}, ${
    n(data.runs, 'run')} and ${n(data.compilations, 'compilation')}.`;
  loadQueue().catch(() => {}); // the sidebar's counts
}

async function showSettings() {
  const config = await api('/settings');
  if (state.view.name !== 'settings') return;
  const hasKey = config.secrets_configured.includes('llm_api_key');
  main.innerHTML = `<form class="settings" id="settings-form" novalidate>
    <h1>Settings</h1>
    <p class="hint">An empty field uses its default, shown in grey.</p>
    <fieldset><legend>Subscriptions</legend>
      <div class="trio">
        ${field('subscription_check_hours', config, { type: 'number' })}
        ${field('subscription_min_minutes', config, { type: 'number' })}
      </div>
      <p class="hint">How often each <a href="/subscriptions" data-link>subscribed channel</a> is checked for new
        uploads and finished livestreams, and the shortest video a check keeps. 0 hours checks only when you
        subscribe or press Check now.</p>
    </fieldset>
    <fieldset><legend>Clips</legend>
      <div class="trio">
        ${field('min_clip_seconds', config, { type: 'number' })}
        ${field('max_clip_seconds', config, { type: 'number' })}
        ${field('max_clips', config, { type: 'number' })}
      </div>
      ${field('candidates_per_window', config, { type: 'number' })}
      <p class="hint">How many moments Highlightr proposes in each 10-minute stretch before ranking them all
        together and keeping the best (up to Max clips). More gives it more to choose from; 1–10.</p>
      <label class="field instructions-field" for="f-clip_instructions">
        <span class="label-row"><span>${FIELDS.clip_instructions}</span></span>
        <textarea id="f-clip_instructions" name="clip_instructions" rows="3" maxlength="1000"
          placeholder="${esc(config.defaults.clip_instructions)}">${esc(shown('clip_instructions', config))}</textarea>
      </label>
      <p class="hint">Sent to Highlightr as its "preferred style" when picking and ranking clips.
        You can override it per link.</p>
    </fieldset>
    <fieldset><legend>TV</legend>
      ${field('feed_mix', config)}
      <p class="hint">How often <a href="/tv" data-link>TV</a> picks clips made in the last 3, 7, 30 and 90 days,
        then any clip at all: five weights from 0 to 100, newest first. Within each, clips TV has played less come up more.</p>
    </fieldset>
    <fieldset><legend>Transcription</legend>
      ${selectField('youtube_captions', config, CAPTION_OPTIONS)}
      <p class="hint">Using YouTube's transcript skips the audio download and Parakeet entirely.
        Parakeet is still used when a video has no transcript in its spoken language.</p>
      ${field('parakeet_url', config, { type: 'url', test: 'parakeet' })}
      ${field('parakeet_model', config)}
    </fieldset>
    <fieldset><legend>Highlights · Highlightr</legend>
      ${field('highlightr_url', config, { type: 'url', test: 'highlightr' })}
      ${field('llm_endpoint', config, { type: 'url', test: 'llm' })}
      ${field('llm_model', config, { placeholder: "The endpoint's default model" })}
      ${field('llm_api_key', config, { type: 'password', placeholder: 'Optional' })}
      ${hasKey ? '<label class="field"><span class="label-row"><span><input type="checkbox" id="clear-key"> Remove saved API key</span></span></label>' : ''}
    </fieldset>
    <fieldset><legend>Search · Embeddings</legend>
      ${field('embed_url', config, { type: 'url', test: 'embeddings' })}
      ${field('embed_model', config)}
      <p class="hint">An OpenAI-compatible <code>/v1/embeddings</code> server for searching clips by meaning.
        New clips are embedded after each run; changing the model re-embeds everything in the background.</p>
    </fieldset>
    <fieldset><legend>Appearance</legend>
      <label class="field" for="f-color_mode">
        <span class="label-row"><span>Mode</span></span>
        <select id="f-color_mode" name="color_mode">${MODE_OPTIONS.map(([v, label]) =>
    `<option value="${v}" ${v === colorMode() ? 'selected' : ''}>${esc(label)}</option>`).join('')}</select>
      </label>
      <label class="field" for="f-nav_layout">
        <span class="label-row"><span>Navigation</span></span>
        <select id="f-nav_layout" name="nav_layout">${NAV_OPTIONS.map(([v, label]) =>
    `<option value="${v}" ${v === navLayout() ? 'selected' : ''}>${esc(label)}</option>`).join('')}</select>
      </label>
      <p class="hint">Both are kept in this browser and applied right away, without Save. On a phone the nav is always
        across the top.</p>
    </fieldset>
    <div class="settings-footer">
      <button type="submit" class="box-button small" id="save-settings">Save</button>
      <span class="status" id="settings-status" role="status"></span>
      <a class="docs" href="/api/docs" target="_blank">API docs →</a>
    </div>
  </form>
  <section class="settings clear-all" aria-labelledby="clear-title">
    <fieldset><legend id="clear-title">Clear all</legend>
      <p class="hint">Deletes every episode, transcript, run (queued and in progress too), clip and its thumbnail,
        compilation, speaker and TV play, for a fresh start. Subscriptions, the videos they found and these
        settings stay. This can't be undone.</p>
      <div class="clear-row">
        <button type="button" class="box-button small danger" id="clear-all">Clear all clips…</button>
        <span class="status" id="clear-status" role="status"></span>
      </div>
    </fieldset>
  </section>`;

  const form = $('#settings-form');
  form.elements.color_mode.addEventListener('change', (event) => setColorMode(event.target.value));
  form.elements.nav_layout.addEventListener('change', (event) => setNavLayout(event.target.value));
  $('#clear-all').addEventListener('click', clearAll);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const status = $('#settings-status');
    const update = {};
    for (const key of Object.keys(FIELDS)) {
      const input = form.elements[key];
      if (!input || input.disabled) continue;
      if (key === 'llm_api_key') {
        if ($('#clear-key')?.checked) update[key] = '';
        else if (input.value.trim()) update[key] = input.value.trim();
        continue;
      }
      update[key] = input.value.trim();
    }
    $('#save-settings').disabled = true;
    status.textContent = 'Saving…';
    try {
      await api('/settings', { method: 'PUT', body: JSON.stringify(update) });
      await labs.refresh().catch(() => {}); // a lab switched on or off shows or hides in the Episodes tab
      await showSettings();
      $('#settings-status').textContent = 'Saved.';
    } catch (error) {
      status.textContent = error.message;
      $('#save-settings').disabled = false;
    }
  });

  form.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-test]');
    if (!button) return;
    const service = button.dataset.test;
    const result = $(`#r-${service}`);
    result.className = 'result';
    result.textContent = 'Testing…';
    button.disabled = true;
    // An empty field means its default, so that's what gets tested.
    const value = (key) => form.elements[key].value.trim() || config.defaults[key];
    try {
      const body = { service };
      if (service === 'llm') {
        body.url = value('llm_endpoint');
        body.model = value('llm_model');
        // A typed key wins; otherwise the server uses the saved one unless it's being removed.
        const key = form.elements.llm_api_key.value.trim();
        if (key) body.api_key = key;
        else if ($('#clear-key')?.checked) body.api_key = '';
      } else if (service === 'embeddings') {
        body.url = value('embed_url');
        body.model = value('embed_model');
      } else {
        body.url = value(`${service}_url`);
      }
      const outcome = await api('/settings/test', { method: 'POST', body: JSON.stringify(body) });
      result.className = `result ${outcome.ok ? 'ok' : 'bad'}`;
      result.textContent = outcome.ok ? 'Connected' : outcome.message;
      result.title = outcome.message;
    } catch (error) {
      result.className = 'result bad';
      result.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
}

// --- submit modal ------------------------------------------------------------

const modal = $('#submit-modal');
const urlInput = $('#submit-url');
const submitError = $('#submit-error');

const submitButton = $('#submit-button');
const lookupBox = $('#lookup');
let lookupTimer;
let lookupController;
let lookupInfo = null; // the video the card shows, once checked
// Submit stays disabled from the moment a link is typed until its caption check settles.
let checking = false;
let submitting = false;

function syncSubmit() {
  submitButton.disabled = checking || submitting;
}

function setChecking(value) {
  checking = value;
  syncSubmit();
}

function resetLookup() {
  clearTimeout(lookupTimer);
  lookupController?.abort();
  setChecking(false);
  lookupBox.hidden = true;
  lookupBox.innerHTML = '';
  lookupInfo = null;
}

const looksLikeYouTube = (value) => /youtu\.?be|^[\w-]{11}$/.test(value);

const VERDICTS = {
  youtube_auto: 'Will use the auto-generated captions, so no transcription needed.',
  youtube: "Will use the creator's captions, so no transcription needed.",
  parakeet: 'No captions in its spoken language, so it will be transcribed with Parakeet.',
};

// Subscribe to the video's channel before clipping it, or say it's followed already.
function lookupSubscription(info) {
  if (info.subscription) return '<span class="lookup-subscribed">✓ Subscribed</span>';
  if (!info.channel_id) return '';
  return `<button type="button" class="link-button" id="lookup-subscribe"
    title="Find this channel's new long uploads on the Subscriptions page">Subscribe</button>`;
}

function lookupCard(info) {
  const chip = (language, label) => `<span class="chip ${language ? 'yes' : 'no'}">${language ? '✓' : '✕'} ${label}${
    language ? ` · ${esc(language)}` : ''}</span>`;
  const hasCaptions = info.captions.auto || info.captions.creator;
  const verdict = info.transcript_source === 'parakeet' && hasCaptions
    ? 'Captions are available, but Settings say to transcribe with Parakeet.'
    : VERDICTS[info.transcript_source];
  const thumb = `<img class="lookup-thumb" alt="" src="${esc(info.thumbnail_url)}">`;
  return `${httpsUrl(info.url) ? `<a class="lookup-thumb-link thumb-box" href="${esc(info.url)}" target="_blank"
      rel="noopener" title="Watch on YouTube" aria-label="Watch on YouTube">${thumb}</a>`
    : `<span class="lookup-thumb-link thumb-box">${thumb}</span>`}
    <div class="lookup-body">
      <p class="lookup-title">${esc(info.title || info.video_id)}</p>
      <p class="lookup-meta">${[esc(info.channel || ''), info.duration_ms ? clock(info.duration_ms / 1000) : '',
    lookupSubscription(info)].filter(Boolean).join(' · ')}</p>
      <p class="sub-error lookup-sub-error" id="lookup-sub-error" role="alert"></p>
      <p class="chips">${chip(info.captions.auto, 'Auto-generated captions')}${chip(info.captions.creator, 'Creator captions')}</p>
      <p class="lookup-verdict">${verdict}</p>
      ${info.runs.length
    ? `<p class="lookup-verdict muted">Already clipped (<a href="/r/${esc(info.runs[0].id)}" data-link>open it</a>).
        Submitting clips it again, reusing the transcript.</p>`
    : ''}
    </div>`;
}

async function runLookup(url) {
  lookupController = new AbortController();
  const { signal } = lookupController;
  lookupBox.hidden = false;
  lookupBox.innerHTML = '<p class="lookup-status"><span class="spinner" aria-hidden="true"></span>Checking YouTube for captions…</p>';
  try {
    const info = await api(`/lookup?url=${encodeURIComponent(url)}`, { signal });
    if (!signal.aborted) {
      lookupInfo = info;
      lookupBox.innerHTML = lookupCard(info);
    }
  } catch (error) {
    if (!signal.aborted) lookupBox.innerHTML = `<p class="lookup-status bad">${esc(error.message)}</p>`;
  } finally {
    // An aborted check was superseded; the newer one owns the flag.
    if (!signal.aborted) setChecking(false);
  }
}

urlInput.addEventListener('input', () => {
  submitError.textContent = '';
  resetLookup();
  const url = urlInput.value.trim();
  if (looksLikeYouTube(url)) {
    setChecking(true);
    lookupTimer = setTimeout(() => runLookup(url), 350);
  }
});

// Subscribe from the modal: the video's link names its channel, as on a run's page.
lookupBox.addEventListener('click', async (event) => {
  if (event.target.closest('a[data-link]')) { // "open it": close the modal; the link handler opens the run
    closeModal();
    return;
  }
  const button = event.target.closest('#lookup-subscribe');
  const info = lookupInfo;
  if (!button || !info) return;
  button.disabled = true;
  button.textContent = 'Subscribing…';
  $('#lookup-sub-error').textContent = '';
  const { ok, data } = await apiRaw('/subscriptions', {
    method: 'POST', body: JSON.stringify({ url: info.url, ...followBody('uploads') }),
  }).catch((failure) => ({ ok: false, data: { detail: failure.message } }));
  if (ok) {
    loadQueue().catch(() => {}); // the sidebar's count
    if (state.view?.name === 'subscriptions' && state.view.drawn) refreshSubscriptions({ force: true }).catch(() => {});
  }
  if (lookupInfo !== info) return; // closed, or another link was typed meanwhile
  if (!ok) {
    button.disabled = false;
    button.textContent = 'Subscribe';
    $('#lookup-sub-error').textContent = problemText(data) || 'Couldn’t subscribe.';
    return;
  }
  info.subscription = { id: data.id, uploads: data.uploads, streams: data.streams };
  button.outerHTML = lookupSubscription(info);
});

const instructionsInput = $('#submit-instructions');

// Clip options for this link only (POST /runs `options`), behind the arrow: Settings → Clips' fields, each left
// empty for its setting (shown in grey). Closed and emptied each time the modal opens.
const RUN_OPTIONS = ['min_clip_seconds', 'max_clip_seconds', 'max_clips', 'candidates_per_window'];
const optionsToggle = $('#submit-options-toggle');
const optionsBox = $('#submit-options');
optionsBox.innerHTML = `<div class="option-grid">${RUN_OPTIONS.map((key) => `<label class="field" for="o-${key}">
    <span class="label-row"><span>${esc(FIELDS[key])}</span></span>
    <input id="o-${key}" name="${key}" type="number" min="1"${key === 'candidates_per_window' ? ' max="10"' : ''}
      step="1" inputmode="numeric" autocomplete="off"></label>`).join('')}</div>
  <p class="hint">For this link only. Empty uses Settings, shown in grey.</p>`;
const optionInput = (key) => $(`#o-${key}`);

function showOptions(open) {
  optionsToggle.setAttribute('aria-expanded', String(open));
  optionsBox.hidden = !open;
}
optionsToggle.addEventListener('click', () => showOptions(optionsBox.hidden));

// The options typed in, as whole numbers ({} for none); the range checks are the API's. Throws, with the
// field opened and focused, on one that isn't a whole number.
function runOptions() {
  const options = {};
  for (const key of RUN_OPTIONS) {
    const input = optionInput(key);
    const value = input.value.trim();
    if (!value && !input.validity.badInput) continue;
    const n = Number(value);
    if (input.validity.badInput || !Number.isInteger(n)) {
      showOptions(true);
      input.focus();
      throw new Error(`${FIELDS[key]} must be a whole number.`);
    }
    options[key] = n;
  }
  return options;
}

// What a failed POST /runs says, naming the clip option each of its problems is about.
function runProblemText(data, status) {
  const errors = data?.errors || [];
  if (errors.some((e) => e.pointer?.startsWith('/options/'))) showOptions(true);
  const text = errors.map((e) => {
    const key = e.pointer?.match(/^\/options\/(\w+)$/)?.[1];
    return key && FIELDS[key] ? `${FIELDS[key]}: ${e.message}` : e.message;
  }).join(' ');
  return text || problemText(data) || `Request failed (${status})`;
}

let modalOpener = null;
let modalStay = null; // { view, video }: a found video's Clip, pressed on the Subscriptions page, stays there

// `url` prefills the link (e.g. a video a subscription found) and checks it right away. `foundVideo`
// (its YouTube id) keeps the Subscriptions page shown after submitting, rather than going to the run.
function openModal(url = '', { foundVideo = null } = {}) {
  modalStay = foundVideo ? { view: state.view, video: foundVideo } : null;
  if (!modal.open) {
    const active = document.activeElement;
    modalOpener = active && active !== document.body ? active : null;
    modal.showModal();
  }
  submitError.textContent = '';
  urlInput.value = url;
  instructionsInput.value = '';
  for (const key of RUN_OPTIONS) optionInput(key).value = '';
  showOptions(false);
  resetLookup();
  if (url && looksLikeYouTube(url)) {
    setChecking(true);
    runLookup(url);
  }
  (url ? instructionsInput : urlInput).focus();
  // Show the Settings values as the placeholders; blank means "use Settings".
  api('/settings').then((config) => {
    instructionsInput.placeholder = `Default: ${config.values.clip_instructions}`;
    for (const key of RUN_OPTIONS) optionInput(key).placeholder = config.values[key] || 'No limit';
  }).catch(() => {});
}

function closeModal() {
  if (modal.open) modal.close();
}

// However it closes (x, Escape, a click outside, a submit): stop the check, and put focus back.
modal.addEventListener('close', () => {
  resetLookup();
  const back = modalOpener?.isConnected ? modalOpener : $('#open-submit');
  modalOpener = null;
  back.focus();
});

$('#open-submit').addEventListener('click', () => openModal());
$('#close-submit').addEventListener('click', closeModal);
modal.addEventListener('click', (event) => { if (event.target === modal) closeModal(); });
document.addEventListener('click', (event) => {
  const opener = event.target.closest('[data-open-submit]');
  if (!opener) return;
  const card = opener.closest('#sub-videos [data-video]');
  openModal(opener.dataset.openSubmit || '', { foundVideo: card?.dataset.video });
});

$('#submit-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (checking || submitting) return; // Enter key can still fire while the button is disabled.
  const url = urlInput.value.trim();
  if (!url) {
    submitError.textContent = 'Paste a YouTube link.';
    return;
  }
  submitting = true;
  syncSubmit();
  submitError.textContent = '';
  try {
    const instructions = instructionsInput.value.trim();
    const options = runOptions();
    const { ok, status, data: run } = await apiRaw('/runs', {
      method: 'POST', body: JSON.stringify({ url, instructions, ...(Object.keys(options).length ? { options } : {}) }),
    });
    if (!ok) throw new Error(runProblemText(run, status));
    const stay = modalStay && stillOn(modalStay.view, 'subscriptions') ? modalStay : null;
    closeModal();
    await loadQueue();
    if (!stay) {
      navigate(`/r/${run.id}`);
      return;
    }
    // The found video's card follows the new run where it is (even under "Not clipped yet": it goes at
    // the next full reload), and focus goes back to it.
    const video = stay.view.videos?.find((v) => v.video_id === stay.video);
    if (video) {
      video.run = { id: run.id, status: run.status, clip_count: run.clip_count };
      video.episode_id = run.episode.id;
    }
    await refreshSubscriptions({ videos: false });
    if (!stillOn(stay.view, 'subscriptions')) return;
    $(`#sub-videos [data-video="${CSS.escape(stay.video)}"] .card-open`)?.focus();
  } catch (error) {
    submitError.textContent = error.message;
  } finally {
    submitting = false;
    syncSubmit();
  }
});

// Retry a failed or cancelled run, or cancel one that's queued or processing.
main.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-retry], [data-cancel]');
  if (!button) return;
  const action = button.dataset.retry ? 'retry' : 'cancel';
  const id = button.dataset.retry || button.dataset.cancel;
  if (action === 'cancel' && !confirm('Cancel this run? Retrying it later reuses the transcript if it was finished.')) return;
  button.disabled = true;
  try {
    await api(`/runs/${encodeURIComponent(id)}/${action}`, { method: 'POST' });
    await loadQueue();
    await showRun(id);
  } catch (error) {
    button.disabled = false;
    alert(error.message);
  }
});

// --- subscriptions: followed channels and the long videos found on them --------------
//
// A channel rail (filterable, so hundreds of channels stay manageable) beside the found videos. Picking
// a channel changes the URL (?channel=) but not the page around it: the rail keeps its filter, scroll
// and focus, and only the channel's controls and videos are drawn again.

const FOLLOW_OPTIONS = [['uploads', 'Uploads'], ['streams', 'Finished livestreams'], ['both', 'Both']];
const followOf = (sub) => (sub.uploads && sub.streams ? 'both' : sub.uploads ? 'uploads' : 'streams');
const followBody = (value) => ({ uploads: value !== 'streams', streams: value !== 'uploads' });
const KIND_OPTIONS = [['', 'All videos'], ['upload', 'Uploads'], ['stream', 'Livestreams']];
const RAIL_SHOW = [['all', 'All channels'], ['waiting', 'With videos to clip'], ['autoclip', 'Auto-clipping'],
  ['failed', 'Check failed'], ['checking', 'Waiting for a check']];

const rail = { text: '', show: 'all' }; // the rail's filter, kept between visits to the page
let shownSubs = null; // the subscriptions the drawn page shows, so picking a channel needn't fetch them
let bulk = null; // { stop } while a pasted list is being subscribed to

async function allSubscriptions() {
  const out = [];
  let cursor = null;
  do {
    const page = await api(`/subscriptions?limit=200${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`);
    out.push(...page.data);
    cursor = page.next_cursor;
  } while (cursor);
  return out;
}

const waitingCount = (subs) => subs.reduce((n, s) => n + s.unclipped_count, 0);
const checkFailed = (sub) => Boolean(sub.check_error) && !sub.check_pending;
const RAIL_TESTS = {
  all: () => true,
  waiting: (s) => s.unclipped_count > 0,
  autoclip: (s) => s.autoclip,
  failed: checkFailed,
  checking: (s) => s.check_pending,
};
// Case and accents don't matter when filtering channels by name or handle.
const fold = (text) => String(text ?? '').normalize('NFD').replace(/\p{M}/gu, '').toLowerCase();

function scheduleLine(config) {
  const hours = Number(config.values.subscription_check_hours) || 0;
  const minutes = Number(config.values.subscription_min_minutes) || 0;
  const when = hours ? `Every ${hours === 1 ? 'hour' : `${hours} hours`}` : 'When you subscribe or press Check';
  return `${when}, Yolt looks at each channel's newest videos and keeps up to three of each kind${
    minutes ? ` that run ${minutes} minutes or longer` : ''}. Channels with Auto-clip on queue a run for each new one; the
    rest wait until you press Clip.`;
}

function subscriptionStatus(sub) {
  const spinner = '<span class="spinner" aria-hidden="true"></span>';
  if (sub.check_pending && !sub.checked_at) return `${spinner}Waiting for its first check…`;
  const found = sub.video_count
    ? `${sub.video_count} found${sub.unclipped_count ? `, ${sub.unclipped_count} not clipped` : ', all clipped'}`
    : 'nothing long enough yet';
  if (sub.check_pending) return `${spinner}Waiting to be checked · ${found}`;
  const next = sub.next_check_at ? ` · next ${ago(sub.next_check_at)}` : '';
  if (sub.check_error) {
    return `<span class="bad">Check failed ${ago(sub.checked_at)}: ${esc(sub.check_error)}</span>${next} · ${found}`;
  }
  return `Checked ${ago(sub.checked_at)}${next} · ${found}`;
}

// This page with another channel (or all of them, ''), keeping the video filters.
function channelHref(channel) {
  const { kind, unclipped } = state.view.filters;
  const query = new URLSearchParams();
  if (kind) query.set('kind', kind);
  if (unclipped) query.set('unclipped', '1');
  if (channel) query.set('channel', channel);
  return `/subscriptions${query.toString() ? `?${query}` : ''}`;
}

function railItem({ channel, name, count, dot = '', title = name, selected }) {
  return `<li><a class="rail-item${channel ? '' : ' all'}" href="${esc(channelHref(channel))}" data-link
    data-channel="${esc(channel)}" title="${esc(title)}" ${selected ? 'aria-current="page"' : ''}><span
    class="rail-dot ${dot}" aria-hidden="true"></span><span class="name">${esc(name)}</span><span
    class="n">${count || ''}</span></a></li>`;
}

function subRailItem(sub, selected) {
  const dot = sub.check_pending ? 'pending' : checkFailed(sub) ? 'failed' : '';
  const title = [sub.name, sub.handle, checkFailed(sub) ? `check failed: ${sub.check_error}` : '',
    sub.check_pending ? 'waiting for a check' : '', sub.autoclip ? 'auto-clips new videos' : '',
    sub.unclipped_count ? `${sub.unclipped_count} not clipped` : ''].filter(Boolean).join(' · ');
  return railItem({ channel: String(sub.podcast_id), name: sub.name, count: sub.unclipped_count, dot, title, selected });
}

// Replace a box's contents only when they changed, keeping focus on the same control if it had it.
function redraw(box, html, keyAttribute) {
  if (!box || box.dataset.html === html) return;
  const focused = box.contains(document.activeElement) ? document.activeElement.getAttribute(keyAttribute) : null;
  box.innerHTML = html;
  box.dataset.html = html;
  if (focused !== null) $(`[${keyAttribute}="${CSS.escape(focused)}"]`, box)?.focus({ preventScroll: true });
}

function renderRail() {
  const { view } = state;
  const subs = view.subs;
  const list = $('#rail-list');
  if (!list || !subs) return;
  const words = fold(rail.text).split(/\s+/).filter(Boolean);
  const test = RAIL_TESTS[rail.show] || RAIL_TESTS.all;
  const shown = subs.filter((s) => test(s) && words.every((w) => fold(`${s.name} ${s.handle ?? ''}`).includes(w)));
  const { channel } = view.filters;
  const empty = !subs.length ? 'No channels yet: subscribe to one above.'
    : !shown.length ? 'No channels match.' : '';
  redraw(list, railItem({ channel: '', name: 'All channels', count: waitingCount(subs), selected: !channel })
    + shown.map((s) => subRailItem(s, String(s.podcast_id) === channel)).join('')
    + (empty ? `<li class="empty">${empty}</li>` : ''), 'data-channel');

  $('#rail-total').textContent = subs.length || '';
  $('#rail-filter').placeholder = `Filter ${subs.length === 1 ? 'the channel' : `${subs.length} channels`}`;
  for (const option of $('#rail-show').options) {
    const label = `${RAIL_SHOW.find(([value]) => value === option.value)[1]} (${subs.filter(RAIL_TESTS[option.value]).length})`;
    if (option.textContent !== label) option.textContent = label;
  }
  const pending = subs.filter((s) => s.check_pending).length;
  const failed = subs.filter(checkFailed).length;
  const notes = [
    pending ? `<span class="spinner" aria-hidden="true"></span><span title="Channels are checked one at a time">Checking ${
      pending === 1 ? '1 channel' : `${pending} channels`}…</span>` : '',
    failed && rail.show !== 'failed' ? `<button type="button" class="link-button" data-rail-show="failed">${failed} failed</button>` : '',
  ].filter(Boolean);
  redraw($('#rail-checking'), notes.join(' · '), 'data-rail-show');
}

// Above the videos: the chosen channel's status and controls, or a summary of every channel.
function renderDetail() {
  const { view } = state;
  const subs = view.subs;
  const { channel } = view.filters;
  const sub = channel && subs.find((s) => String(s.podcast_id) === channel);
  const close = `<a class="detail-close" href="${esc(channelHref(''))}" data-link>All channels</a>`;
  let head;
  let status;
  let actions = '';
  if (!channel) {
    const found = subs.reduce((n, s) => n + s.video_count, 0);
    head = '<h2 class="label">Found videos</h2>';
    status = subs.length ? `${found} found on ${subs.length === 1 ? '1 channel' : `${subs.length} channels`}${
      found ? `, ${waitingCount(subs)} not clipped` : ''}` : '';
  } else if (!sub) {
    head = `<div class="detail-title"><h2 class="label">Found videos from an unsubscribed channel</h2>${close}</div>`;
    status = 'Not followed any more; the videos found before stay here.';
  } else {
    const name = httpsUrl(sub.url)
      ? `<a href="${esc(sub.url)}" target="_blank" rel="noopener" title="Open the channel on YouTube">${esc(sub.name)}</a>`
      : esc(sub.name);
    head = `<div class="detail-title">
        <h2 class="detail-name">${name}${sub.handle ? `<span class="handle">${esc(sub.handle)}</span>` : ''}</h2>${close}
      </div>`;
    actions = `<div class="detail-actions">
        <select data-follow="${esc(sub.id)}" data-focus-key="follow" aria-label="What to follow from ${esc(sub.name)}">${FOLLOW_OPTIONS.map(([value, label]) =>
    `<option value="${value}" ${value === followOf(sub) ? 'selected' : ''}>${label}</option>`).join('')}</select>
        <label class="check" title="Each check queues a run, with the default focus, for every video it finds from now on">
          <input type="checkbox" data-autoclip="${esc(sub.id)}" data-focus-key="autoclip" ${sub.autoclip ? 'checked' : ''}>
          Auto-clip new videos</label>
        <button type="button" class="box-button small" data-check="${esc(sub.id)}" data-focus-key="check" ${sub.check_pending ? 'disabled' : ''}
          title="Look for new videos on this channel now">${sub.check_pending ? 'Check asked for' : 'Check now'}</button>
        <button type="button" class="link-button" data-unsubscribe="${esc(sub.id)}" data-name="${esc(sub.name)}"
          data-focus-key="unsubscribe">Unsubscribe</button>
      </div>`;
    status = subscriptionStatus(sub);
  }
  redraw($('#detail-head'), head, 'data-none');
  redraw($('#detail-status'), status, 'data-none');
  redraw($('#detail-actions'), actions, 'data-focus-key');
}

// New subscriptions from a check or a subscribe: what the page shows, and the sidebar's count.
function setSubs(subs) {
  const { view } = state;
  view.subs = subs;
  shownSubs = subs;
  view.pending = subs.some((s) => s.check_pending);
  const sub = view.filters.channel && subs.find((s) => String(s.podcast_id) === view.filters.channel);
  setTitle(view.filters.channel ? `${sub ? sub.name : 'Unsubscribed channel'} · Subscriptions` : 'Subscriptions');
  renderRail();
  renderDetail();
  if (state.queue.totals) state.queue.totals.subscriptions = subs.length;
  renderSidebar();
}

// A found video's run, as a link to it. The rest of the card opens the clip popup.
function videoAction(video) {
  const { run } = video;
  if (!run) return '';
  if (isActive(run)) {
    return `<a class="box-button small busy" href="/r/${esc(run.id)}" data-link title="Follow the run"><span class="spinner"
      aria-hidden="true"></span>${esc(runStage(run))}</a>`;
  }
  if (run.status === 'ready') {
    return `<a class="box-button small added" href="/r/${esc(run.id)}" data-link>${run.clip_count
      ? `${run.clip_count} ${run.clip_count === 1 ? 'clip' : 'clips'}` : 'No clips'}</a>`;
  }
  return `<a class="last-run bad" href="/r/${esc(run.id)}" data-link>Last run ${esc(run.status)}</a>`;
}

// Where in its cycle a card's loading sweep starts (0-17 tenths of its 1.8s), from the video id: fixed per
// card, and spread out, so a page of them shimmers out of step.
const sweepDelay = (id) => [...id].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7) % 18;

function videoCard(video) {
  const status = !video.run ? '' : isActive(video.run) ? 'clipping' : video.run.status === 'ready' ? 'clipped' : '';
  const badge = status === 'clipped' ? '<span class="badge clipped">✓ Clipped</span>'
    : status === 'clipping' ? `<span class="badge">${video.run.status === 'queued' ? 'Queued' : 'Clipping'}</span>` : '';
  const meta = [video.kind === 'stream' ? '<span class="kind">Livestream</span>' : '',
    video.published_at ? esc(ago(video.published_at)) : ''].filter(Boolean).join(' · ');
  const action = videoAction(video);
  const picked = selection.has(video.video_id);
  // The title is the card's button; it stretches over the whole card (CSS), under the run's link and
  // the select toggle. While anything is selected, a click anywhere on a card selects it instead.
  return `<div class="video-card ${status}${picked ? ' picked' : ''}" data-video="${esc(video.video_id)}">
    <button type="button" class="card-select" data-select="${esc(video.video_id)}" aria-pressed="${picked}"
      aria-label="Select ${esc(video.title)}" title="Select to clip several at once (Shift: a range)"><span
      aria-hidden="true">✓</span></button>
    <div class="thumb-box" style="--sweep:${sweepDelay(video.video_id)}">
      <img class="thumb" loading="lazy" alt="" src="${esc(video.thumbnail_url)}">${badge}
      ${video.duration_ms ? `<span class="duration">${clock(video.duration_ms / 1000)}</span>` : ''}
    </div>
    <button type="button" class="card-open" data-open-submit="${esc(video.url)}" title="${esc(video.title)}"
      aria-label="${status === 'clipped' ? 'Clip again' : 'Clip'}: ${esc(video.title)}"><span
      class="video-title">${esc(video.title)}</span></button>
    ${state.view.filters.channel ? '' : `<p class="from">${esc(video.channel.name)}</p>`}
    <p class="card-meta">${meta}</p>
    ${action ? `<div class="video-action">${action}</div>` : ''}
  </div>`;
}

// Draw view.videos, card by card (syncChildren): a refresh only touches the cards that changed.
function renderVideos() {
  const { view } = state;
  const box = $('#sub-videos');
  if (!box || !view.videos) return;
  if (!view.videos.length) {
    const filtered = view.filters.kind || view.filters.unclipped;
    redraw(box, `<p class="meta-line">${filtered ? 'No found videos match.' : 'No videos found yet.'}</p>`, 'data-none');
    renderBatchBar();
    return;
  }
  let grid = $('.clip-grid', box);
  if (!grid) {
    box.innerHTML = '<div class="clip-grid recent videos"></div><div class="more"></div>';
    box.dataset.html = ''; // the empty state's markup: drawn again if the list empties
    grid = $('.clip-grid', box);
  }
  syncChildren(grid, view.videos, (v) => v.video_id, videoCard);
  const more = $('.more', box);
  const cursor = view.nextCursor || '';
  if (more.dataset.cursor !== cursor) {
    more.dataset.cursor = cursor;
    more.innerHTML = cursor ? `<button type="button" class="box-button small" data-more="${esc(cursor)}"
      data-auto-more>More</button>` : '';
  }
  renderBatchBar();
}

// The first page of found videos replaces what's shown; `cursor` adds the next one (scrolling near the
// end presses its More button).
async function loadFoundVideos(cursor = null) {
  const { view } = state;
  const { kind, unclipped, channel } = view.filters;
  const query = new URLSearchParams({ limit: '24' });
  if (kind) query.set('kind', kind);
  if (unclipped) query.set('clipped', 'false');
  if (channel) query.set('podcast_id', channel);
  const wasPaged = view.paged;
  if (cursor) {
    if (view.loadingMore) return;
    view.loadingMore = true;
    query.set('cursor', cursor);
    view.paged = true; // from now on new finds don't replace the list; the runs on it still update
  }
  let page;
  try {
    page = await api(`/subscriptions/videos?${query}`);
  } catch (error) {
    if (cursor && state.view === view) view.paged = wasPaged; // nothing was added after all
    throw error;
  } finally {
    if (cursor) view.loadingMore = false;
  }
  if (state.view !== view || !$('#sub-videos')) return;
  if (cursor) {
    const shown = new Set(view.videos.map((v) => v.video_id));
    view.videos.push(...page.data.filter((v) => !shown.has(v.video_id)));
  } else {
    if (view.paged) return; // a later page arrived meanwhile; this one is already on the list
    view.videos = page.data;
  }
  view.nextCursor = page.next_cursor;
  renderVideos();
}

// The runs of the videos shown, as the queue has them now; the ones that left it, as they ended.
async function followVideoRuns() {
  const { view } = state;
  if (!view.videos) return;
  const ended = followQueue(view.videos.map((v) => v.run));
  renderVideos(); // a going run's stage comes from the queue too
  if (!ended.length) return;
  const now = await endedRuns(ended);
  if (state.view !== view) return;
  ended.forEach((run, i) => {
    if (now[i]) Object.assign(run, { status: now[i].status, clip_count: now[i].clip_count });
  });
  renderVideos();
}

// --- clipping several found videos at once ---------------------------------------------

// Picked videos (YouTube id -> the found video), kept while you move between channels; a video leaves
// once it's queued, or with Clear. Runs still go one at a time, oldest queued first.
const selection = new Map();
let lastPicked = null; // what a Shift-click selects a range from
let batch = null; // { stop, done, total } while a selection is being queued
let batchNote = ''; // how the last batch went, until dismissed or another one starts

const clippable = (v) => !v.run || !(isActive(v.run) || v.run.status === 'ready');

function togglePick(videoId, { range = false } = {}) {
  const videos = state.view.videos || [];
  const target = videos.find((v) => v.video_id === videoId);
  if (!target || batch) return;
  const on = !selection.has(videoId);
  let span = [target];
  const from = range && lastPicked ? videos.findIndex((v) => v.video_id === lastPicked) : -1;
  if (from >= 0) {
    const to = videos.indexOf(target);
    span = videos.slice(Math.min(from, to), Math.max(from, to) + 1);
  }
  for (const v of span) {
    if (on) selection.set(v.video_id, v);
    else selection.delete(v.video_id);
  }
  lastPicked = videoId;
  batchNote = '';
  renderVideos();
}

function renderBatchBar() {
  const bar = $('#sub-batch');
  if (!bar) return;
  const n = selection.size;
  $('#sub-videos')?.classList.toggle('picking', n > 0 || Boolean(batch));
  bar.hidden = !n && !batch && !batchNote;
  if (bar.hidden) return;
  const more = (state.view.videos || []).filter((v) => clippable(v) && !selection.has(v.video_id)).length;
  $('#batch-count').textContent = batch ? `Queueing ${Math.min(batch.done + 1, batch.total)} of ${batch.total}…`
    : n ? `${n} selected` : '';
  $('#batch-clip').textContent = batch ? 'Stop' : `Clip ${n}`;
  $('#batch-clip').hidden = !n && !batch;
  $('#batch-focus').hidden = !n || Boolean(batch);
  $('#batch-all').hidden = Boolean(batch) || !n || !more;
  $('#batch-all').textContent = `Select all ${more} not clipped`;
  $('#batch-clear').hidden = Boolean(batch) || !n;
  const note = $('#batch-note');
  if (note.innerHTML !== batchNote) note.innerHTML = batchNote;
  $('#batch-dismiss').hidden = !batchNote || n > 0 || Boolean(batch);
}

// Queue every picked video with the focus typed in the bar (blank: the default), one request at a time
// (each reads the video's details from YouTube first).
async function clipPicked() {
  const picked = [...selection.values()];
  if (!picked.length || batch) return;
  const instructions = $('#batch-focus').value.trim();
  batch = { stop: false, done: 0, total: picked.length };
  batchNote = '';
  renderBatchBar();
  const failed = [];
  let queued = 0;
  for (const video of picked) {
    if (batch.stop) break;
    const { ok, data } = await apiRaw('/runs', { method: 'POST', body: JSON.stringify({ url: video.url, instructions }) })
      .catch((failure) => ({ ok: false, data: { detail: failure.message } }));
    batch.done += 1;
    if (ok) {
      queued += 1; // a new run, even for a video clipped before
      // The card wherever it's drawn: the picked object, and the shown one if that's another.
      const shown = (state.view.videos || []).filter((v) => v.video_id === video.video_id);
      for (const v of new Set([video, ...shown])) {
        v.run = { id: data.id, status: data.status, clip_count: data.clip_count };
        v.episode_id = data.episode.id;
      }
      selection.delete(video.video_id);
      await loadQueue().catch(() => {}); // the card's place in the queue, the sidebar's count
    } else {
      failed.push(`${esc(video.title)}: ${esc(problemText(data) || 'couldn’t be queued')}`);
    }
    if (state.view?.name === 'subscriptions') renderVideos();
  }
  const left = batch.total - batch.done;
  batch = null;
  batchNote = [
    queued ? `Queued ${queued} ${queued === 1 ? 'video' : 'videos'}; they run one at a time
      (<a href="/episodes?status=active" data-link>Episodes</a>).` : '',
    left ? `Stopped with ${left} still selected.` : '',
    failed.length ? `<span class="bad">${failed.length} couldn’t be queued:</span> ${failed.join('; ')}` : '',
  ].filter(Boolean).join(' ');
  if (state.view?.name === 'subscriptions' && state.view.drawn) {
    renderVideos();
    await refreshSubscriptions({ videos: false }).catch(() => {});
  }
}

// What found videos a list of subscriptions has: when it changes, the videos are fetched again.
const foundKey = (subs) => subs.map((s) => `${s.id}:${s.video_count}:${s.unclipped_count}`).join();

// Called while checks are pending or runs are going: the channels' status, then the videos: the first
// page again if a check found some (unless more pages are showing), else just their runs.
async function refreshSubscriptions({ force = false, videos = true } = {}) {
  const { view } = state;
  const before = view.subs ? foundKey(view.subs) : null;
  const subs = await allSubscriptions();
  if (!stillOn(view, 'subscriptions') || !view.drawn) return; // left, or not drawn yet
  setSubs(subs);
  if (videos && !view.paged && (force || foundKey(subs) !== before)) await loadFoundVideos();
  else await followVideoRuns();
}

async function showSubscriptions(params) {
  const kind = KIND_OPTIONS.some(([value]) => value && value === params.get('kind')) ? params.get('kind') : '';
  const unclipped = params.get('unclipped') === '1';
  const channel = /^\d+$/.test(params.get('channel') || '') ? params.get('channel') : '';
  const { view } = state;
  view.filters = { kind, unclipped, channel };

  // Another channel or filter on the page already drawn: keep the rail as it is.
  if ($('#sub-layout') && shownSubs) {
    view.drawn = true;
    const form = $('#sub-filter');
    form.elements.kind.value = kind;
    form.elements.unclipped.checked = unclipped;
    const more = $('#sub-videos .more');
    if (more) { // the last list's next page isn't this one's
      more.replaceChildren();
      more.dataset.cursor = '';
    }
    setSubs(shownSubs);
    await loadFoundVideos();
    refreshSubscriptions().catch(() => {}); // the counts as they are now, without holding up the videos
    return;
  }

  const [subs, config] = await Promise.all([allSubscriptions(), api('/settings')]);
  if (!stillOn(view, 'subscriptions')) return;
  main.innerHTML = `<h1 class="page-title">Subscriptions</h1>
    <form class="sub-add" id="sub-form" novalidate>
      <textarea name="urls" rows="1" maxlength="30000" autocomplete="off" spellcheck="false" aria-describedby="sub-error"
        placeholder="@handle, channel or video link, or UC… id (or paste a list)"
        aria-label="Channels to subscribe to"></textarea>
      <select name="follow" aria-label="What to follow">${FOLLOW_OPTIONS.map(([value, label]) =>
    `<option value="${value}">${label}</option>`).join('')}</select>
      <button type="submit" class="box-button small">Subscribe</button>
    </form>
    <p class="sub-error" id="sub-error" role="alert"></p>
    <p class="sub-progress" id="sub-progress" aria-live="polite"></p>
    <p class="meta-line">${scheduleLine(config)} <a href="/settings" data-link>Settings</a></p>
    <div class="sub-layout" id="sub-layout">
      <nav class="sub-rail" aria-label="Subscribed channels">
        <div class="rail-head">
          <h2 class="label">Channels <span class="count" id="rail-total"></span></h2>
          <button type="button" class="link-button" id="sub-check" title="Look for new videos on every channel now">Check all</button>
        </div>
        <input type="search" id="rail-filter" autocomplete="off" spellcheck="false" aria-label="Filter channels"
          aria-controls="rail-list" value="${esc(rail.text)}">
        <select id="rail-show" aria-label="Which channels to list">${RAIL_SHOW.map(([value, label]) =>
    `<option value="${value}" ${value === rail.show ? 'selected' : ''}>${label}</option>`).join('')}</select>
        <p class="rail-checking" id="rail-checking" aria-live="polite"></p>
        <ul class="rail-list" id="rail-list"></ul>
        <div class="rail-foot">
          <a class="link-button" href="/api/v1/subscriptions/export" download
            title="Download every subscription as a JSON file, to import here or in another Yolt">Export</a>
          <button type="button" class="link-button" id="sub-import"
            title="Subscribe to the channels in an exported JSON file">Import…</button>
          <input type="file" id="sub-import-file" accept=".json,application/json" hidden>
        </div>
      </nav>
      <section class="sub-pane" aria-label="Found videos">
        <div class="sub-detail" id="sub-detail">
          <div id="detail-head"></div>
          <p class="sub-status" id="detail-status"></p>
          <div id="detail-actions"></div>
        </div>
        <form class="search-form" id="sub-filter">
          <select name="kind" aria-label="Which videos">${KIND_OPTIONS.map(([value, label]) =>
    `<option value="${value}" ${value === kind ? 'selected' : ''}>${label}</option>`).join('')}</select>
          <label class="check"><input type="checkbox" name="unclipped" ${unclipped ? 'checked' : ''}> Not clipped yet</label>
        </form>
        <div id="sub-videos"></div>
        <div class="batch-bar" id="sub-batch" hidden>
          <span class="batch-count" id="batch-count" aria-live="polite"></span>
          <input type="text" id="batch-focus" maxlength="1000" autocomplete="off"
            placeholder="Default: ${esc(config.values.clip_instructions)}" aria-label="What should the clips focus on? (optional)">
          <button type="button" class="box-button small" id="batch-clip"></button>
          <button type="button" class="link-button" id="batch-all"></button>
          <button type="button" class="link-button" id="batch-clear">Clear</button>
          <p class="batch-note" id="batch-note" aria-live="polite"></p>
          <button type="button" class="link-button" id="batch-dismiss" aria-label="Dismiss">×</button>
        </div>
      </section>
    </div>`;
  view.drawn = true;
  setSubs(subs);
  $('#rail-list [aria-current="page"]:not(.all)')?.scrollIntoView({ block: 'center' });
  bindSubscriptionPage();
  await loadFoundVideos();
}

// The page's controls. Bound once per drawing and kept while channels are picked, so they act on
// whichever subscriptions view is current, not the one they were bound in.
function bindSubscriptionPage() {
  const onPage = () => state.view?.name === 'subscriptions' && state.view.drawn;
  const form = $('#sub-form');
  const urls = form.elements.urls;
  const grow = () => {
    urls.style.height = 'auto';
    urls.style.height = `${Math.min(urls.scrollHeight + 2, 180)}px`;
  };
  urls.addEventListener('input', grow);
  urls.addEventListener('keydown', (event) => { // Enter subscribes; Shift+Enter starts another line
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (bulk) bulk.stop = true; // the button reads Stop while a list is going
    else subscribeAll(form, grow);
  });

  $('#sub-check').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await api('/subscriptions/check', { method: 'POST' });
      if (onPage()) await refreshSubscriptions();
    } catch (failure) {
      alert(failure.message);
    } finally {
      button.disabled = false;
    }
  });

  $('#sub-import').addEventListener('click', () => $('#sub-import-file').click());
  $('#sub-import-file').addEventListener('change', (event) => {
    const input = event.currentTarget;
    const [file] = input.files;
    input.value = ''; // so picking the same file again still imports it
    if (file) importSubscriptions(file);
  });

  const filter = $('#rail-filter');
  filter.addEventListener('input', () => {
    rail.text = filter.value;
    if (onPage()) renderRail();
  });
  filter.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { // the first channel listed
      event.preventDefault();
      $('#rail-list .rail-item:not(.all)')?.click();
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      $('#rail-list .rail-item')?.focus();
    } else if (event.key === 'Escape' && filter.value) {
      event.preventDefault();
      filter.value = '';
      filter.dispatchEvent(new Event('input'));
    }
  });
  $('#rail-list').addEventListener('keydown', (event) => { // arrows move along the list, up from the top to the filter
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    const items = [...document.querySelectorAll('#rail-list .rail-item')];
    const index = items.indexOf(document.activeElement);
    if (index < 0) return;
    event.preventDefault();
    const next = event.key === 'ArrowDown' ? items[index + 1] : items[index - 1];
    (next || (event.key === 'ArrowUp' ? filter : null))?.focus();
  });
  const show = $('#rail-show');
  const setShow = (value) => {
    rail.show = value;
    show.value = value;
    if (onPage()) renderRail();
  };
  show.addEventListener('change', () => setShow(show.value));
  $('#rail-checking').addEventListener('click', (event) => {
    const button = event.target.closest('[data-rail-show]');
    if (button) setShow(button.dataset.railShow);
  });

  const detail = $('#sub-detail');
  detail.addEventListener('change', async (event) => {
    const control = event.target.closest('[data-follow], [data-autoclip]');
    if (!control) return;
    control.disabled = true;
    const id = control.dataset.follow ?? control.dataset.autoclip;
    const body = control.dataset.follow ? followBody(control.value) : { autoclip: control.checked };
    const { ok, data } = await apiRaw(`/subscriptions/${encodeURIComponent(id)}`, {
      method: 'PATCH', body: JSON.stringify(body),
    }).catch((failure) => ({ ok: false, data: { detail: failure.message } }));
    if (!ok) alert(problemText(data) || 'Couldn’t change the subscription.');
    $('#detail-actions').dataset.html = ''; // redraw even if nothing changed, putting back a refused choice
    if (onPage()) await refreshSubscriptions();
  });
  detail.addEventListener('click', async (event) => {
    const check = event.target.closest('[data-check]');
    const unsubscribe = event.target.closest('[data-unsubscribe]');
    if (check) {
      check.disabled = true;
      const { ok, data } = await apiRaw(`/subscriptions/${encodeURIComponent(check.dataset.check)}/check`, {
        method: 'POST',
      }).catch((failure) => ({ ok: false, data: { detail: failure.message } }));
      if (!ok) {
        check.disabled = false;
        alert(problemText(data) || 'Couldn’t ask for a check.');
      } else if (onPage()) {
        await refreshSubscriptions();
      }
    } else if (unsubscribe) {
      if (!confirm(`Unsubscribe from ${unsubscribe.dataset.name}? Videos found so far stay listed.`)) return;
      unsubscribe.disabled = true;
      const { ok, data } = await apiRaw(`/subscriptions/${encodeURIComponent(unsubscribe.dataset.unsubscribe)}`, {
        method: 'DELETE',
      }).catch((failure) => ({ ok: false, data: { detail: failure.message } }));
      if (!ok) {
        unsubscribe.disabled = false;
        alert(problemText(data) || 'Couldn’t unsubscribe.');
        return;
      }
      if (!onPage()) return;
      shownSubs = shownSubs.filter((s) => String(s.id) !== unsubscribe.dataset.unsubscribe);
      navigate(channelHref(''), { replace: true });
    }
  });

  $('#sub-filter').addEventListener('change', (event) => {
    if (!onPage()) return;
    const { elements } = event.currentTarget;
    state.view.filters = { ...state.view.filters, kind: elements.kind.value, unclipped: elements.unclipped.checked };
    navigate(channelHref(state.view.filters.channel));
  });

  // Selecting: the toggle on a card, or anywhere on one while any is selected (not the clip popup then).
  $('#sub-videos').addEventListener('click', (event) => {
    const card = event.target.closest('.video-card');
    if (!card || !onPage() || event.target.closest('.video-action a')) return;
    if (!event.target.closest('[data-select]') && !selection.size) return;
    event.preventDefault();
    event.stopPropagation();
    togglePick(card.dataset.video, { range: event.shiftKey });
  });
  $('#batch-clip').addEventListener('click', () => {
    if (batch) batch.stop = true; // after the one being queued
    else clipPicked();
  });
  $('#batch-focus').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.isComposing) clipPicked();
  });
  $('#batch-all').addEventListener('click', () => {
    for (const v of state.view.videos || []) if (clippable(v)) selection.set(v.video_id, v);
    batchNote = '';
    renderVideos();
  });
  $('#batch-clear').addEventListener('click', () => {
    selection.clear();
    renderVideos();
  });
  $('#batch-dismiss').addEventListener('click', () => {
    batchNote = '';
    renderBatchBar();
  });

  // One listener for every More (each page's button replaces the last), off while its page loads.
  $('#sub-videos').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-more]');
    if (!button || button.disabled || !onPage()) return;
    button.disabled = true;
    try {
      await loadFoundVideos(button.dataset.more);
    } catch (error) {
      moreFailed(button, error);
    }
  });
}

// Subscribe to the channels in an export file (GET /subscriptions/export), all at once: the file names each
// channel by its id, so YouTube isn't asked first. Channels already followed are left as they are.
async function importSubscriptions(file) {
  const error = $('#sub-error');
  const progress = $('#sub-progress');
  error.textContent = '';
  progress.textContent = '';
  let body = null;
  try {
    body = JSON.parse(await file.text());
  } catch {
    // shown below
  }
  const list = Array.isArray(body?.subscriptions) ? body.subscriptions : null;
  if (!list) {
    error.textContent = `${file.name} isn’t a subscriptions file. Use one from Export.`;
    return;
  }
  const known = new Set((shownSubs || []).map((s) => s.channel_id));
  const listed = new Map();
  for (const sub of list) if (!listed.has(sub?.channel_id)) listed.set(sub?.channel_id, sub);
  const fresh = [...listed.values()].filter((sub) => !known.has(sub?.channel_id));
  if (!fresh.length) {
    progress.textContent = list.length ? `You already follow every channel in ${file.name}.` : `${file.name} lists no channels.`;
    return;
  }
  const autoclip = fresh.filter((sub) => sub?.autoclip).length;
  const plural = (n) => `${n} ${n === 1 ? 'channel' : 'channels'}`;
  const already = listed.size - fresh.length;
  if (!confirm([`Subscribe to ${plural(fresh.length)} from ${file.name}?`,
    already === 1 ? 'One you already follow stays as it is.' : already ? `${already} you already follow stay as they are.` : '',
    !autoclip ? '' : fresh.length === 1 ? 'It auto-clips: its first check queues a run for each video it finds.'
      : `${autoclip === fresh.length ? 'All' : autoclip} of them auto-clip: their first check queues a run for each video it finds.`,
  ].filter(Boolean).join('\n\n'))) return;

  const button = $('#sub-import');
  button.disabled = true;
  progress.textContent = `Importing ${plural(fresh.length)}…`;
  const { ok, data } = await apiRaw('/subscriptions/import', { method: 'POST', body: JSON.stringify(body) })
    .catch((failure) => ({ ok: false, data: { detail: failure.message } }));
  if (!$('#sub-import')) return; // left the page
  button.disabled = false;
  if (!ok) {
    progress.textContent = '';
    // Which channel each problem is about, by its name in the file.
    const where = (e) => {
      const at = /^\/subscriptions\/(\d+)/.exec(e.pointer || '');
      const sub = at && list[Number(at[1])];
      return at ? `${sub?.name || sub?.channel_id || `Entry ${Number(at[1]) + 1}`}: ` : '';
    };
    const errors = data?.errors || [];
    error.innerHTML = errors.length
      ? errors.slice(0, 8).map((e) => esc(where(e) + e.message)).join('<br>')
        + (errors.length > 8 ? `<br>…and ${errors.length - 8} more.` : '')
        + '<br>Nothing was imported.'
      : esc(problemText(data) || 'Couldn’t import the file.');
    return;
  }
  progress.textContent = [`Subscribed to ${plural(data.created)}`,
    data.already_subscribed ? `${data.already_subscribed} already followed` : ''].filter(Boolean).join(', ')
    + '. Each is checked for new videos in turn.';
  if (state.view?.name === 'subscriptions' && state.view.drawn) await refreshSubscriptions({ force: true }).catch(() => {});
}

// Subscribe to every channel in the box, one at a time (each takes a second or two: Yolt reads the
// channel's page). What didn't work goes back in the box, with why, to fix and try again.
async function subscribeAll(form, grow) {
  const entries = [...new Set(form.elements.urls.value.split(/[\s,]+/).filter(Boolean))];
  const error = () => $('#sub-error');
  const progress = () => $('#sub-progress');
  const button = () => $('#sub-form button[type="submit"]');
  if (!entries.length) {
    error().textContent = 'Paste a channel or video link, or an @handle.';
    return;
  }
  const many = entries.length > 1;
  const follow = followBody(form.elements.follow.value);
  bulk = { stop: false };
  error().textContent = '';
  button().textContent = many ? 'Stop' : 'Subscribing…';
  button().disabled = !many;
  const failed = [];
  let added = 0;
  let already = 0;
  let done = 0;
  let last = null;
  for (const url of entries) {
    if (bulk.stop) break;
    if (many) progress().textContent = `Subscribing to ${done + 1} of ${entries.length}: ${url}`;
    const { ok, status, data } = await apiRaw('/subscriptions', {
      method: 'POST', body: JSON.stringify({ url, ...follow }),
    }).catch((failure) => ({ ok: false, data: { detail: failure.message } }));
    done += 1;
    if (ok) {
      if (status === 201) added += 1;
      else already += 1;
      last = data;
      if (many && state.view?.name === 'subscriptions' && state.view.drawn) {
        await refreshSubscriptions().catch(() => {}); // the rail fills in as it goes
      }
    } else {
      failed.push({ url, message: problemText(data) || 'Couldn’t subscribe.' });
    }
  }
  const left = entries.slice(done);
  bulk = null;
  const box = $('#sub-form')?.elements.urls;
  if (box) {
    box.value = [...failed.map((f) => f.url), ...left].join('\n');
    grow();
  }
  if (button()) {
    button().textContent = 'Subscribe';
    button().disabled = false;
  }
  if (error()) error().innerHTML = failed.map((f) => `${esc(f.url)}: ${esc(f.message)}`).join('<br>');
  if (progress()) {
    progress().textContent = !many ? '' : [
      `Subscribed to ${added} ${added === 1 ? 'channel' : 'channels'}`,
      already ? `${already} already followed` : '',
      failed.length ? `${failed.length} couldn’t be added` : '',
      left.length ? `stopped with ${left.length} to go` : '',
    ].filter(Boolean).join(', ') + '.';
  }
  if (state.view?.name !== 'subscriptions' || !state.view.drawn) return;
  if (!many && last) {
    // The new channel, waiting for its first check. Its videos appear as the check finds them.
    shownSubs = [...shownSubs.filter((s) => s.id !== last.id), last].sort((a, b) => fold(a.name).localeCompare(fold(b.name)));
    navigate(channelHref(String(last.podcast_id)));
  } else {
    await refreshSubscriptions({ force: true }).catch(() => {});
  }
}

// Escape lets go of the selected found videos (when not typing, and no popup is open).
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape' || !selection.size || batch || modal.open || state.view?.name !== 'subscriptions') return;
  if (event.target.closest?.('input, textarea, select')) return;
  selection.clear();
  renderVideos();
});

// --- episodes: every run, newest first ---------------------------------------------

const EPISODE_SHOW = [['', 'All runs'], ['active', 'In progress'], ['ready', 'Ready'], ['failed', 'Failed'],
  ['cancelled', 'Cancelled']];

function episodeStatus(run) {
  if (isActive(run)) {
    return `<span class="going"><span class="spinner" aria-hidden="true"></span>${esc(runStage(run))}</span>`;
  }
  if (run.status === 'ready') return `${run.clip_count} ${run.clip_count === 1 ? 'clip' : 'clips'}`;
  return run.status === 'failed' ? '<span class="bad">Failed</span>' : '<span class="muted">Cancelled</span>';
}

function episodeRow(run) {
  const { episode } = run;
  const thumb = videoThumb(episode.source);
  const when = isActive(run) ? `added ${ago(run.created_at)}`
    : `${run.status === 'ready' ? 'clipped' : run.status} ${ago(run.finished_at || run.created_at)}`;
  const meta = [episode.podcast, episode.duration_ms ? clock(episode.duration_ms / 1000) : '', when].filter(Boolean);
  const action = isActive(run) ? `<button type="button" class="link-button" data-cancel-run="${esc(run.id)}">Cancel</button>`
    : run.status !== 'ready' ? `<button type="button" class="link-button" data-retry-run="${esc(run.id)}">Retry</button>` : '';
  // The title's link covers the row (CSS), below the action buttons.
  return `<li class="ep-row ${esc(run.status)}">
    <span class="thumb-box ep-thumb${thumb ? '' : ' failed'}" style="--sweep:${sweepDelay(String(run.id))}">${
      thumb ? `<img class="thumb" loading="lazy" alt="" src="${esc(thumb)}">` : ''}</span>
    <div class="ep-main">
      <a class="ep-open" href="/r/${esc(run.id)}" data-link><span class="ep-title">${esc(episode.title)}</span></a>
      <p class="ep-meta">${meta.map(esc).join(' · ')}</p>
      ${run.instructions ? `<p class="ep-focus" title="${esc(run.instructions)}">Focus: ${esc(run.instructions)}</p>` : ''}
      ${run.status === 'failed' && run.error ? `<p class="ep-error" title="${esc(run.error)}">${esc(run.error)}</p>` : ''}
    </div>
    <div class="ep-status">${episodeStatus(run)}</div>
    <div class="ep-actions">${action}${clipAgainButton(episode, 'link-button')}</div>
  </li>`;
}

function renderEpisodes() {
  const { view } = state;
  const list = $('#ep-list');
  if (!list || !view.runs) return;
  syncChildren(list, view.runs, (r) => r.id, episodeRow);
  $('#ep-empty').hidden = view.runs.length > 0;
  const more = $('#ep-more');
  const cursor = view.nextCursor || '';
  if (more.dataset.cursor !== cursor) {
    more.dataset.cursor = cursor;
    more.innerHTML = cursor ? `<button type="button" class="box-button small" data-more="${esc(cursor)}"
      data-auto-more>More</button>` : '';
  }
  const { processing, queued } = state.queue;
  redraw($('#ep-queue'), [
    processing.length ? `Now: <a href="/r/${esc(processing[0].id)}" data-link>${esc(processing[0].episode.title)}</a>
      (${esc(runStage(processing[0]))})` : '',
    queued.length ? `${queued.length} queued, one at a time ·
      <button type="button" class="link-button" id="ep-cancel-queued">Cancel all queued</button>` : '',
  ].filter(Boolean).join(' · '), 'id');
}

// The first page replaces the list; `cursor` adds the next one (scrolling near the end asks for it).
async function loadEpisodes(cursor = null) {
  const { view } = state;
  const query = new URLSearchParams({ limit: '25' });
  for (const status of view.show === 'active' ? ['queued', 'processing'] : view.show ? [view.show] : []) {
    query.append('status', status);
  }
  const wasPaged = view.paged;
  if (cursor) {
    if (view.loadingMore) return;
    view.loadingMore = true;
    view.paged = true;
    query.set('cursor', cursor);
  }
  let page;
  try {
    page = await api(`/runs?${query}`);
  } catch (error) {
    if (cursor && state.view === view) view.paged = wasPaged;
    throw error;
  } finally {
    if (cursor) view.loadingMore = false;
  }
  if (!stillOn(view, 'episodes')) return;
  if (cursor) {
    const shown = new Set(view.runs.map((r) => r.id));
    view.runs.push(...page.data.filter((r) => !shown.has(r.id)));
  } else {
    if (view.paged) return; // a later page arrived meanwhile
    view.runs = page.data;
  }
  view.nextCursor = page.next_cursor;
  renderEpisodes();
}

// While runs are going: each row as the queue has it, the ended ones fetched, and (unless more pages
// are showing) the first page again when the queue changed, for runs queued from elsewhere.
async function followEpisodeRuns({ changed }) {
  const { view } = state;
  if (!view.runs) return;
  view.runs = view.runs.map((r) => processingRun(r.id) || r); // its stage and progress
  const ended = followQueue(view.runs);
  renderEpisodes();
  if (ended.length) {
    const now = (await endedRuns(ended)).filter(Boolean);
    if (state.view !== view) return;
    view.runs = view.runs.map((r) => now.find((n) => n.id === r.id) || r);
    renderEpisodes();
  }
  if (changed && !view.paged) await loadEpisodes();
}

async function showEpisodes(params) {
  const { view } = state;
  view.show = EPISODE_SHOW.some(([value]) => value && value === params.get('status')) ? params.get('status') : '';
  main.innerHTML = `${libraryTabs('episodes')}<h1 class="page-title">Episodes</h1>
    <form class="search-form ep-filter" id="ep-filter">
      <select name="status" aria-label="Which runs">${EPISODE_SHOW.map(([value, label]) =>
    `<option value="${value}" ${value === view.show ? 'selected' : ''}>${label}</option>`).join('')}</select>
      <span class="ep-queue" id="ep-queue"></span>
    </form>
    <ul class="ep-list" id="ep-list"></ul>
    <p class="meta-line" id="ep-empty" hidden>${view.show ? 'No runs match.' : 'Nothing clipped yet.'}</p>
    <div class="more" id="ep-more"></div>`;

  $('#ep-filter').addEventListener('change', (event) => {
    const status = event.currentTarget.elements.status.value;
    navigate(`/episodes${status ? `?status=${status}` : ''}`);
  });
  $('#ep-queue').addEventListener('click', async (event) => {
    const button = event.target.closest('#ep-cancel-queued');
    const ids = state.queue.queued.map((q) => q.id);
    if (!button || !ids.length || !confirm(`Cancel ${ids.length} queued ${ids.length === 1 ? 'run' : 'runs'}? Retrying one later starts it again.`)) return;
    button.disabled = true;
    for (const id of ids) await api(`/runs/${id}/cancel`, { method: 'POST' }).catch(() => {});
    await loadQueue().catch(() => {});
    if (stillOn(view, 'episodes')) {
      view.paged = false;
      await loadEpisodes();
    }
  });
  $('#ep-list').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-cancel-run], [data-retry-run]');
    if (!button) return;
    const cancel = Boolean(button.dataset.cancelRun);
    const id = button.dataset.cancelRun || button.dataset.retryRun;
    if (cancel && processingRun(Number(id)) && !confirm('Cancel this run? Retrying it later reuses the transcript if it was finished.')) return;
    button.disabled = true;
    try {
      const run = await api(`/runs/${encodeURIComponent(id)}/${cancel ? 'cancel' : 'retry'}`, { method: 'POST' });
      await loadQueue();
      if (!stillOn(view, 'episodes')) return;
      view.runs = view.runs.map((r) => (r.id === run.id ? run : r));
      renderEpisodes();
    } catch (error) {
      button.disabled = false;
      alert(error.message);
    }
  });
  $('#ep-more').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-more]');
    if (!button || button.disabled || !stillOn(view, 'episodes')) return;
    button.disabled = true;
    try {
      await loadEpisodes(button.dataset.more);
    } catch (error) {
      moreFailed(button, error);
    }
  });
  await loadEpisodes();
}

// --- search --------------------------------------------------------------------

const SEARCH_MODES = [['hybrid', 'Words + meaning'], ['keyword', 'Exact words'], ['semantic', 'Meaning only']];

// The search page's address for the form's values. A theme is a filter, not words: it's carried as its ID
// and shown as a chip in the search box, so what's typed searches within it.
function searchPath(form, { q = form.elements.q.value.trim(), like = null, theme = null } = {}) {
  const next = new URLSearchParams();
  if (q) next.set('q', q);
  if (!like && form.elements.mode.value !== 'hybrid') next.set('mode', form.elements.mode.value);
  if (form.elements.unused.checked) next.set('unused', '1');
  if (like) next.set('like', like);
  if (theme) next.set('theme', theme);
  return `/search${next.toString() ? `?${next}` : ''}`;
}

async function showSearch(params) {
  const q = params.get('q') || '';
  const mode = params.get('mode') || 'hybrid';
  const unused = params.get('unused') === '1';
  const like = params.get('like');
  const theme = params.get('theme');
  const themesOpen = params.get('themes') === '1';
  const { view } = state;
  let context = '';
  if (like) {
    const clip = clipCache.get(Number(like)) || await api(`/clips/${encodeURIComponent(like)}`).catch(() => null);
    context = `More like “${esc(clip?.title || `clip ${like}`)}”`;
  }
  const label = theme ? await themeLabelFor(theme) : null;
  if (!stillOn(view, 'search')) return;
  const chip = theme ? `<span class="theme-chip" title="Theme: ${esc(label || theme)}"><span>${esc(label || 'Theme')}</span>
      <button type="button" id="clear-theme" aria-label="Remove the theme and search every clip">×</button></span>` : '';
  main.innerHTML = `${libraryTabs('search')}<h1 class="page-title">Search</h1>
    <form class="search-form" id="search-form" role="search">
      <div class="search-box">${chip}<input type="search" name="q" value="${esc(q)}" maxlength="300" ${like ? 'disabled' : ''}
        placeholder="${like ? 'Searching by similarity' : theme ? 'Search within this theme…' : 'An idea, a phrase, a name…'}"
        aria-label="${theme ? `Search clips in the theme ${esc(label || theme)}` : 'Search clips'}"></div>
      <select name="mode" aria-label="How to match" ${like ? 'disabled' : ''}>${SEARCH_MODES.map(([value, label]) =>
    `<option value="${value}" ${value === mode ? 'selected' : ''}>${label}</option>`).join('')}</select>
      <label class="check"><input type="checkbox" name="unused" ${unused ? 'checked' : ''}> Never used</label>
      <button type="submit" class="box-button small">Search</button>
      <button type="button" class="box-button small" id="themes-toggle" aria-controls="themes-panel"
        aria-expanded="${themesOpen}">Themes</button>
    </form>
    ${context ? `<p class="meta-line">${context} · <a href="/search" data-link>clear</a></p>` : ''}
    <div id="embed-notice"></div>
    <section class="themes-panel" id="themes-panel" aria-label="Themes" ${themesOpen ? '' : 'hidden'}></section>
    <div id="search-results"></div>`;
  if (like || mode !== 'keyword') {  // meaning is part of this search: say if some clips can't be found by it
    const box = $('#embed-notice');
    api('/').then((info) => {
      if (box.isConnected) box.innerHTML = embeddingNotice(info, info.embeddings.pending_clips);
    }).catch(() => {});
  }
  const form = $('#search-form');
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    navigate(searchPath(form, { like, theme }));
  });
  $('#clear-theme')?.addEventListener('click', () => navigate(searchPath(form, { like })));

  // The themes panel opens in place, so the results under it stay; the address keeps it open for a reload.
  const panel = $('#themes-panel');
  const toggle = $('#themes-toggle');
  const results = $('#search-results');
  const searching = Boolean(q || like || theme);
  const setThemesOpen = (open) => {
    toggle.setAttribute('aria-expanded', String(open));
    panel.hidden = !open;
    results.hidden = open && !searching; // the empty page's help is about what the panel shows
    const shown = $('#theme-form', panel);
    themesAddress(open ? { k: shown?.elements.k.value, sort: shown?.elements.sort.value } : null);
    if (open && !panel.childElementCount) loadThemes(panel, {}, form);
  };
  toggle.addEventListener('click', () => setThemesOpen(panel.hidden));
  if (themesOpen) {
    results.hidden = !searching;
    loadThemes(panel, { k: params.get('k'), sort: params.get('sort') }, form);
  }
  if (!searching) {
    results.innerHTML = `<div class="empty-state"><div>
      <p>Searches every clip. “Words + meaning” finds exact names and terms, and paraphrases of the idea.</p>
      <p><button type="button" class="link-button" id="browse-themes">Browse themes</button> to see what the
        catalogue is about.</p></div></div>`;
    $('#browse-themes').addEventListener('click', () => {
      setThemesOpen(true);
      toggle.focus();
    });
    return;
  }
  const query = new URLSearchParams({ limit: '24' });
  if (like) query.append('like', like);
  else if (q) {
    query.set('q', q);
    if (mode !== 'keyword') query.set('search', mode);
  }
  if (theme) query.set('theme', theme);
  if (unused) query.set('usage', 'never');
  await loadResults(query, q && !like ? q : null);
}

async function loadResults(query, highlightQuery, cursor = null) {
  const box = $('#search-results');
  const page = new URLSearchParams(query);
  if (cursor) page.set('cursor', cursor);
  const { ok, status, data } = await apiRaw(`/clips?${page}`);
  if (!box.isConnected) return;
  if (!ok) {
    // A later page keeps the results already shown; its More button says what went wrong.
    if (cursor) throw new Error(problemText(data) || `search failed (${status})`);
    const hint = status === 503 ? ' Try “Exact words”, which always works.' : '';
    box.innerHTML = `<p class="error-text">${esc(problemText(data) || `Search failed (${status}).`)}${hint}</p>`;
    return;
  }
  let grid = $('.clip-grid', box);
  if (!cursor) {
    box.innerHTML = data.data.length ? '<div class="clip-grid recent"></div><div class="more"></div>'
      : '<p class="meta-line">No clips match.</p>';
    grid = $('.clip-grid', box);
  }
  if (!grid) return;
  grid.insertAdjacentHTML('beforeend', data.data.map((c) => clipCard(c, { from: true, query: highlightQuery })).join(''));
  const more = $('.more', box);
  more.innerHTML = data.next_cursor ? '<button type="button" class="box-button small" data-auto-more>More</button>' : '';
  more.querySelector('button')?.addEventListener('click', (event) => {
    const button = event.currentTarget;
    button.disabled = true; // a second click would append the same page again
    loadResults(query, highlightQuery, data.next_cursor).catch((error) => moreFailed(button, error));
  });
}

// --- themes --------------------------------------------------------------------
// A panel on the search page (?themes=1, with its k and sort). Picking a theme puts it in the search box.

const THEME_SORTS = [['-unused', 'Most unused'], ['coverage', 'Least covered'], ['-clips', 'Biggest'], ['-cohesion', 'Tightest']];

// A theme's ID (k20-123-ab12) means nothing to read, so its chip shows its keywords: kept from the panel, else
// looked up in its clustering, whose k the ID holds (GET /themes is cached server-side). Null for an ID
// that no longer resolves; the search then says why.
const themeLabels = new Map();
const themeLabel = (t) => t.keywords.join(' · ') || 'Untitled theme';

async function themeLabelFor(id) {
  if (!themeLabels.has(id)) {
    const k = /^k(\d+)-/.exec(id)?.[1];
    const list = k ? await api(`/themes?k=${k}`).catch(() => null) : null;
    for (const t of list?.data || []) themeLabels.set(t.id, themeLabel(t));
  }
  return themeLabels.get(id) || null;
}

// Keeps the panel's state in the address without a new page: `null` closes it.
function themesAddress(choice) {
  const next = new URLSearchParams(location.search);
  for (const key of ['themes', 'k', 'sort']) next.delete(key);
  if (choice) {
    next.set('themes', '1');
    if (choice.k) next.set('k', choice.k);
    if (choice.sort && choice.sort !== '-unused') next.set('sort', choice.sort);
  }
  const path = `/search${next.toString() ? `?${next}` : ''}`;
  history.replaceState({}, '', path);
  routedPath = path;
}

let themesLoad = 0; // the latest panel load: a slower earlier one doesn't draw over it

async function loadThemes(panel, choice, searchForm) {
  const load = ++themesLoad;
  panel.innerHTML = '<p class="meta-line">Finding themes…</p>';
  // A clustering can't have more themes than embedded clips, so a small catalogue gets fewer.
  const embedded = await api('/').then((info) => info.embeddings.vectors.clips.embedded).catch(() => 0);
  const sizes = [5, 10, 20, 30, 50].filter((n) => n <= embedded);
  const k = choice.k || String(Math.min(20, Math.max(2, embedded)));
  if (!sizes.includes(Number(k))) sizes.push(Number(k));
  sizes.sort((a, b) => a - b);
  const sort = choice.sort || '-unused';
  const { ok, status, data } = embedded < 2
    ? { ok: false, status: 503, data: { detail: 'Themes need at least two embedded clips. Clip a video, and check Settings → Embeddings.' } }
    : await apiRaw(`/themes?k=${encodeURIComponent(k)}&sort=${encodeURIComponent(sort)}`);
  if (load !== themesLoad || !panel.isConnected) return;
  const controls = `<form class="search-form" id="theme-form">
      <label class="check">Themes <select name="k">${sizes.map((n) =>
    `<option ${String(n) === k ? 'selected' : ''}>${n}</option>`).join('')}</select></label>
      <label class="check">Order <select name="sort">${THEME_SORTS.map(([value, label]) =>
    `<option value="${value}" ${value === sort ? 'selected' : ''}>${label}</option>`).join('')}</select></label>
    </form>`;
  let body;
  if (!ok) {
    body = `<p class="error-text">${esc(problemText(data) || `Themes failed (${status}).`)}</p>`;
  } else {
    for (const t of data.data) themeLabels.set(t.id, themeLabel(t));
    // A card's link is the theme alone; a click also keeps the form's mode and "Never used".
    body = `<p class="meta-line">${data.clip_count} clips in ${data.data.length} themes, grouped by what is said in them.
      Themes aren't named; the keywords and central clips describe them.</p>
      <div class="theme-grid">${data.data.map((t) => `<a class="theme-card" href="/search?theme=${esc(t.id)}"
        data-theme="${esc(t.id)}">
        <p class="keywords">${t.keywords.map(esc).join(' · ') || '—'}</p>
        <ul>${t.representatives.map((r) => `<li>${esc(r.title)}</li>`).join('')}</ul>
        <p class="card-meta">${t.clip_count} clips · ${t.unused_count} unused · ${t.episode_count} episodes${
    t.coverage.compilation_count ? ` · in ${t.coverage.compilation_count} compilation(s)` : ''}</p>
      </a>`).join('')}</div>`;
  }
  panel.innerHTML = controls + body;
  $('#theme-form', panel).addEventListener('change', (event) => {
    const form = event.currentTarget;
    const next = { k: form.elements.k.value, sort: form.elements.sort.value };
    themesAddress(next);
    loadThemes(panel, next, searchForm);
  });
  for (const card of panel.querySelectorAll('[data-theme]')) {
    card.addEventListener('click', (event) => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
      event.preventDefault();
      navigate(searchPath(searchForm, { q: '', theme: card.dataset.theme }));
    });
  }
}

// --- TV ------------------------------------------------------------------------
// Its own module (tv.js and tv.css), loaded the first time a TV is shown (this page, or the home page's),
// with this file's ?v= so it's never stale.

let tvModule = null;
function loadTv() {
  const version = new URL(import.meta.url).search;
  tvModule ??= import(`./tv.js${version}`).then((module) => {
    const style = document.createElement('link');
    style.rel = 'stylesheet';
    style.href = `/static/tv.css${version}`;
    document.head.append(style);
    return module;
  }).catch((error) => {
    tvModule = null; // try again next time
    throw error;
  });
  return tvModule;
}
const tvApp = { api, apiRaw, esc, main, navigate, setTitle, problemText, clock, loadYouTubeApi, slugOf, subtitles };

async function showTv(params) {
  const { view } = state;
  const { show } = await loadTv();
  if (!stillOn(view, 'tv')) return;
  await show(params, tvApp);
}

// --- saved clips (kept in this browser) ------------------------------------------

const SAVED_KEY = 'yolt.saved.v1';

// [{ id, slug }], newest first. The slug guards against a different database reusing an id.
function loadSaved() {
  try {
    const list = JSON.parse(localStorage.getItem(SAVED_KEY));
    return Array.isArray(list) ? list.filter((e) => Number.isInteger(e?.id) && typeof e.slug === 'string') : [];
  } catch {
    return [];
  }
}

let saved = loadSaved();

function storeSaved(next) {
  saved = next;
  localStorage.setItem(SAVED_KEY, JSON.stringify(saved));
  renderSavedCount();
}

window.addEventListener('storage', (event) => { // saved or removed in another tab
  if (event.key !== SAVED_KEY) return;
  saved = loadSaved();
  renderSavedCount();
});

// Saved means this exact clip: an id from another database (different slug) doesn't count.
const isSaved = (clip) => saved.some((e) => e.id === clip.id && e.slug === slugOf(clip));
const saveLabel = (on, page) => (page ? (on ? 'Saved' : 'Save') : (on ? '✓' : '+'));

function renderSavedCount() {
  for (const badge of document.querySelectorAll('.saved-count')) {
    badge.textContent = saved.length ? String(saved.length) : '';
  }
}

// The save toggle: + / ✓ on a clip card, Save / Saved on a clip's page.
function saveButton(clip, { page = false } = {}) {
  const on = isSaved(clip);
  return `<button type="button" class="${page ? 'box-button small' : 'save-clip'}${on ? ' saved' : ''}"
    data-save-clip="${esc(clip.id)}" data-page="${page}" aria-pressed="${on}"
    title="${on ? 'Saved — click to remove' : 'Save'}" aria-label="Save “${esc(clip.title)}”">${saveLabel(on, page)}</button>`;
}

document.addEventListener('click', (event) => {
  const button = event.target.closest('[data-save-clip]');
  if (!button) return;
  event.preventDefault();
  const id = Number(button.dataset.saveClip);
  const clip = clipCache.get(id); // every save button is drawn from a cached clip
  if (!clip) return;
  const on = !isSaved(clip);
  const others = saved.filter((e) => e.id !== id);
  storeSaved(on ? [{ id, slug: slugOf(clip) }, ...others] : others);
  for (const other of document.querySelectorAll(`[data-save-clip="${id}"]`)) {
    other.classList.toggle('saved', on);
    other.setAttribute('aria-pressed', String(on));
    other.title = on ? 'Saved — click to remove' : 'Save';
    other.textContent = saveLabel(on, other.dataset.page === 'true');
  }
  if (!on && state.view?.name === 'saved') {
    button.closest('.clip-cell')?.remove();
    if (!saved.length) showSaved();
  }
});

async function showSaved() {
  const wanted = [...saved];
  const found = [];
  for (let i = 0; i < wanted.length; i += 200) { // a batch fetch takes up to 200 ids
    const query = wanted.slice(i, i + 200).map((e) => `id=${e.id}`).join('&');
    found.push(...(await api(`/clips?${query}`)).data);
  }
  if (state.view.name !== 'saved') return;
  const byId = new Map(found.map((c) => [c.id, c]));
  const clips = wanted.map((e) => byId.get(e.id)).filter((c, i) => c && slugOf(c) === wanted[i].slug);
  if (clips.length !== wanted.length) { // gone from this catalogue: forget them (not ones saved meanwhile)
    const found = new Set(clips.map((c) => `${c.id}:${slugOf(c)}`));
    storeSaved(saved.filter((e) => found.has(`${e.id}:${e.slug}`) || !wanted.includes(e)));
  }
  main.innerHTML = `${libraryTabs('saved')}<h1 class="page-title">Saved</h1>${clips.length
    ? `<p class="meta-line">${clips.length} ${clips.length === 1 ? 'clip' : 'clips'}, newest first. Kept in this browser.</p>
       <div class="clip-grid">${clips.map((c) => clipCard(c, { from: true })).join('')}</div>`
    : '<div class="empty-state"><div><p>Nothing saved yet. Press <b>+</b> on any clip to keep it here.</p></div></div>'}`;
}

// --- speakers: confirm or correct who is in an episode ------------------------------

let editorView = null; // the page the speakers editor was opened on: redraws of that page keep it

main.addEventListener('click', async (event) => {
  const edit = event.target.closest('[data-edit-speakers]');
  if (!edit) return;
  const { view } = state;
  const episode = await api(`/episodes/${edit.dataset.editSpeakers}`);
  const line = $('#speakers-line');
  // Left the page meanwhile, or the editor is open already.
  if (state.view !== view || !line || $('#speakers-form', line)) return;
  editorView = view;
  const row = (s = { name: '', role: 'guest' }) => `<span class="speaker-row">
    <input type="text" maxlength="200" value="${esc(s.name)}" placeholder="Full name" aria-label="Name">
    <select aria-label="Role"><option value="host" ${s.role === 'host' ? 'selected' : ''}>host</option>
      <option value="guest" ${s.role === 'guest' ? 'selected' : ''}>guest</option></select></span>`;
  line.innerHTML = `<form class="speakers-form" id="speakers-form">
    <div class="rows">${(episode.speakers.length ? episode.speakers : [undefined]).map((s) => row(s)).join('')}</div>
    <button type="button" class="link-button" data-add-row>+ person</button>
    <button type="submit" class="box-button small">Save</button>
    <button type="button" class="link-button" data-cancel-speakers>Cancel</button>
    <span class="status" role="status"></span></form>`;
  const form = $('#speakers-form');
  form.addEventListener('click', (e) => {
    if (e.target.closest('[data-add-row]')) $('.rows', form).insertAdjacentHTML('beforeend', row());
    if (e.target.closest('[data-cancel-speakers]')) route();
  });
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const speakers = [...form.querySelectorAll('.speaker-row')].map((r) => ({
      name: $('input', r).value.trim(), role: $('select', r).value })).filter((s) => s.name);
    const { ok, data } = await apiRaw(`/episodes/${episode.id}/speakers`, { method: 'PUT', body: JSON.stringify({ speakers }) });
    if (ok) route();
    else $('.status', form).textContent = problemText(data);
  });
});

// --- polling while runs process ----------------------------------------------

let pollTimer = null;
let polling = false;
const IDLE_QUEUE_MS = 15000;
let queueCheckedAt = Date.now(); // the first look is at startup

async function poll() {
  pollTimer = null;
  if (document.hidden) return;
  polling = true;
  try {
    // The queue every tick while runs are going, and now and then when idle (runs queued or channels
    // subscribed to through the API or from another tab).
    const wasActive = queueBusy();
    let changed = false;
    if (wasActive || Date.now() - queueCheckedAt > IDLE_QUEUE_MS) {
      queueCheckedAt = Date.now();
      changed = await loadQueue();
    }
    const busy = wasActive || changed;
    const { view } = state;
    const runId = Number(view?.id);
    // A run's page refreshes while that run is going (as last shown, or in the queue), or while its
    // clips are waiting to be embedded.
    if (view?.name === 'run' && view.data && (isActive(view.data) || processingRun(runId) || queuePosition(runId) >= 0
      || (view.embedPending && Date.now() >= view.embedNextAt))) {
      await showRun(view.id, { quiet: true });
    } else if (view?.name === 'home' && changed) {
      await refreshHome(view); // a run ended: its clips are the newest (the TV keeps playing)
    } else if (view?.name === 'subscriptions' && (view.pending || busy)) {
      await refreshSubscriptions();
    } else if (view?.name === 'episodes' && busy) {
      await followEpisodeRuns({ changed });
    }
  } catch {
    // Server restarting; try again next tick.
  } finally {
    polling = false;
    if (!document.hidden) pollTimer = setTimeout(poll, POLL_MS);
  }
}

// A hidden tab doesn't poll; showing it again catches up at once.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && !polling && pollTimer === null) poll();
});

// Before the first page, so a lab's page or hook is there for it. Same ?v= as this file, so never stale.
labs = await import(`./labs/index.js${new URL(import.meta.url).search}`)
  .then((module) => module.install({
    api, apiRaw, esc, main, state, stillOn, navigate, setTitle, problemText, clipCard, slugOf, seconds,
    playerMarkup, mountPlayer, homeClip, savedClips: () => saved, libraryTabs,
  }))
  .catch((error) => { console.warn('labs did not load', error); return labs; });

loadQueue().catch(() => {}); // the sidebar's counts too
route();
pollTimer = setTimeout(poll, POLL_MS);
