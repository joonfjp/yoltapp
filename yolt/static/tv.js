// TV: clips playing one after another, like a channel. GET /feed picks them (it favours clips made lately and
// ones the TV hasn't played, and never plays two from one episode in a row while there are others);
// POST /feed/plays counts each play. Two YouTube players take turns, so there's no wait between clips: in a
// clip's last seconds the other one starts the next clip out of sight, and as the clip ends it slides up over
// it, already playing, like scrolling a feed. The picture is the clip page's
// (app.css .player-stage): YouTube's own labels and shelves fall outside it. Its bar sits under it, and the
// transcript is drawn over it as subtitles, a sentence or two lines at a time. Beside it, the clip's quotes
// light up as they're said, over the QR code for the full episode; the keys are under the next clip.
//
// It starts playing as soon as the page opens. Browsers only allow sound once the page has had a click or key
// press, so it starts with sound when it was reached from another Yolt page, and muted when it was opened
// straight from the address bar or a bookmark, until the first click or key press anywhere.
//
// Keys: ↓ / PageDown next clip, ↑ / PageUp previous, Space or K pause, M mute, F full screen.
//
// `compact` is the home page's TV: just the clip's title and the picture under it (tv.css: no episode heading,
// tuning bar, quote, QR code, keys, score, countdown, next clip or YouTube link), playing the whole catalogue;
// the home page links to the full TV beside it. It starts muted (VOL or M for sound), so opening Yolt is quiet, and its keys work only
// while it has focus, so they don't take the page's. Full screen is the full TV (all but the tuning bar): it
// draws everything else too, and tv.css hides it until then.
// The address holds the channel's filters, the same as GET /clips takes (q, usage, theme, speaker_id, …), so
// a channel is a link: /tv?usage=never, /tv?theme=k30-…, /tv?q=jobs.

import qrcode from './vendor/qrcode.mjs';

const BATCH = 8; // clips asked for at a time
const REFILL_AT = 3; // ask for more when this few are left to play
const RECENT = 50; // clips sent as `recent`: the most GET /feed takes
const HISTORY = 30; // clips kept for ↑
const MAX_SKIPS = 5; // clips in a row that won't play before the TV stops trying
const RENEW_EVERY = 50; // clips per YouTube player: a fresh one keeps a long session from bogging down
const STALL_MS = 30000; // a clip whose player does nothing for this long is skipped
const FIRST_START_MS = 4000; // the first clip not started by then: the browser held it back
const END_SLACK = 0.15; // the 150ms tick can't land exactly on the end
const URGENT_SECONDS = 5;
const LEAD = 5; // seconds before a clip's end the next one starts, muted and out of sight
const SLIDE_MS = 420; // the slide from one clip to the next
const SLIDE_WAIT_MS = 2500; // the longest a clip may take to start under the one on screen before sliding in anyway
const ALIGN_SLACK = 0.6; // seconds the next clip may drift from where it should be before it's put back
const UNPLAYABLE_KEY = 'yolt.tv.unplayable.v1'; // episodes whose video YouTube won't play here
const FILTERS = ['q', 'search', 'like', 'min_similarity', 'theme', 'episode_id', 'transcript_id', 'run_id',
  'speaker_id', 'min_score', 'min_duration_ms', 'max_duration_ms', 'usage', 'exclude_id', 'exclude_episode_id'];
const UNPLAYABLE_ERRORS = new Set([100, 101, 150]); // removed or private; its owner doesn't allow embedding
const STATE = { UNSTARTED: -1, ENDED: 0, PLAYING: 1, PAUSED: 2, BUFFERING: 3, CUED: 5 };
const PULL_HOLD_MS = 2500; // a quote stays up this long after it's been said, unless the next one is starting
const SPEAKERS_SHOWN = 4; // pills for who's on a clip, at most: past that, the last says how many more
const PREV_ICON = '<svg viewBox="0 0 12 12" width="10" height="10" aria-hidden="true" focusable="false"><path d="M1 1h1.6v10H1zM11 1v10L3.4 6z" fill="currentColor"/></svg>';
const NEXT_ICON = '<svg viewBox="0 0 12 12" width="10" height="10" aria-hidden="true" focusable="false"><path d="M1 1v10l7.6-5zM9.4 1H11v10H9.4z" fill="currentColor"/></svg>';

const readList = (key) => {
  try {
    const value = JSON.parse(localStorage.getItem(key) || '[]');
    return Array.isArray(value) ? value.filter(Number.isInteger) : [];
  } catch {
    return [];
  }
};
// Whether this page has had a click or key press, so the browser lets it play sound. Without the API, assume not.
const mayPlaySound = () => navigator.userActivation?.hasBeenActive ?? false;

// A quote as a pull quote: "uh" and "um" dropped, a stammer said once ("for for", "that seems that seems"), a
// capital to start, and "…" ending a fragment.
function pullText(text) {
  let out = String(text || '').replace(/(^|\s)(?:uh|um|erm)[,.]?(?=\s|$)/giu, '$1').replace(/\s{2,}/gu, ' ').trim()
    .replace(/(^|\s)((?:[\p{L}\p{N}'’]+\s+){0,2}[\p{L}\p{N}'’]+)(?:\s+\2)+(?=[\s,.?!…]|$)/giu, '$1$2');
  out = out.replace(/^([“"‘'(]*)(\p{Ll})/u, (_, lead, letter) => lead + letter.toUpperCase());
  if (out && !/[.?!…][”’"')\]]*$/u.test(out)) out = `${out.replace(/[,;:\s]+$/u, '')}…`;
  return out;
}

// The clip's quotes in the order they're said, each with when it's said (ms into the video) where its words are
// known. Quotes without words are only used when none have them; a clip without quotes shows none.
function pullQuotes(clip, words) {
  const first = words[0]?.seq;
  const at = (seq) => words[seq - first];
  const quotes = (clip.quotes || []).filter((q) => q.text?.trim());
  const timed = quotes.filter((q) => q.words && at(q.words.first) && at(q.words.last))
    .sort((a, b) => a.words.first - b.words.first)
    .map((q) => ({ text: pullText(q.text), startMs: at(q.words.first).start_ms, endMs: at(q.words.last).end_ms }));
  if (timed.length) return timed;
  return quotes.slice(0, 1).map((q) => ({ text: pullText(q.text), startMs: null, endMs: null }));
}

// Which quote to show `ms` into the video, and how it stands: `coming` (said next), `saying`, or `said` (held a
// moment after, until the next one starts).
function pullAt(pulls, ms) {
  if (!pulls.length) return null;
  if (pulls[0].startMs == null) return { n: 0, state: 'said' };
  for (let n = 0; n < pulls.length; n += 1) {
    const quote = pulls[n];
    const next = pulls[n + 1];
    if (ms < quote.startMs) return { n, state: 'coming' };
    if (ms <= quote.endMs) return { n, state: 'saying' };
    if (!next || ms < Math.min(quote.endMs + PULL_HOLD_MS, next.startMs)) return { n, state: 'said' };
  }
  return { n: pulls.length - 1, state: 'said' };
}

const clipStart = (clip) => clip.time.start_ms / 1000;
const clipEnd = (clip) => clip.time.end_ms / 1000;
const isTyping = (target) => Boolean(target?.closest?.('input, textarea, select, [contenteditable="true"]'));

// The full episode from the clip's moment. The short form keeps the code sparse enough to scan small.
const episodeUrl = (clip) => {
  const start = Math.floor(clipStart(clip));
  return `https://youtu.be/${encodeURIComponent(clip.source.video_id)}${start ? `?t=${start}` : ''}`;
};

// Black on white, one path of unit cells, with a 2-cell quiet zone so phones can find it.
function qrSvg(text) {
  const code = qrcode(0, 'M');
  code.addData(text);
  code.make();
  const count = code.getModuleCount();
  const margin = 2;
  let path = '';
  for (let row = 0; row < count; row += 1) {
    for (let col = 0; col < count; col += 1) {
      if (code.isDark(row, col)) path += `M${col + margin} ${row + margin}h1v1h-1z`;
    }
  }
  const size = count + margin * 2;
  return `<svg viewBox="0 0 ${size} ${size}" shape-rendering="crispEdges" aria-hidden="true" focusable="false">
    <rect width="${size}" height="${size}" fill="#fff"/><path d="${path}" fill="#000"/></svg>`;
}

const dayFormat = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
const day = (value) => {
  const date = value ? new Date(`${value}T00:00:00Z`) : null;
  return date && !Number.isNaN(date.getTime()) ? dayFormat.format(date) : '';
};
const initials = (name) => {
  const words = name.split(/\s+/u).filter(Boolean);
  return (words.length > 1 ? [words[0], words.at(-1)] : words).map((w) => Array.from(w)[0]).join('').toUpperCase();
};

export async function show(params, app, { into = app.main, compact = false } = {}) {
  const { esc, clock } = app;
  if (!compact) app.setTitle('TV');
  const filters = new URLSearchParams();
  for (const [key, value] of params) if (FILTERS.includes(key)) filters.append(key, value);
  const extra = [...new Set([...filters.keys()].filter((key) => key !== 'q'))]; // all but the topic (usage=never too): set by links

  into.innerHTML = `<div class="tv${compact ? ' compact' : ''}" id="tv">
    ${compact ? '' : `<div class="tv-toolbar">
      <form class="tv-tune" id="tv-tune" role="search">
        <input type="search" name="q" value="${esc(filters.get('q') || '')}" placeholder="Topic (exact words)"
          aria-label="Topic" spellcheck="false" autocomplete="off">
        <button type="submit" class="box-button small">Tune in</button>
        ${extra.length ? `<span class="tv-extra">Also filtered by ${esc(extra.join(', '))} ·
          <a href="/tv" data-link>clear</a></span>` : ''}
      </form>
      <div class="tv-actions">
        <button type="button" class="box-button small" data-tv="reshuffle">Reshuffle</button>
        <a class="box-button small" id="tv-open" href="/tv" data-link title="The clip's own page">Open clip</a>
      </div>
    </div>`}
    <section class="tv-screen" id="tv-screen" tabindex="-1" aria-label="TV">
      <header class="tv-masthead" id="tv-masthead"></header>
      <aside class="tv-left">
        <div class="tv-pull" id="tv-pull"></div>
        <div class="tv-qr" id="tv-qr"></div>
        <div class="tv-notes">
          <p class="tv-sound" id="tv-sound" hidden>Muted until you click or press a key: browsers start videos
            without sound.</p>
          <p class="tv-status" id="tv-status" role="status"></p>
          <button type="button" class="box-button small" data-tv="unskip" id="tv-unskip" hidden></button>
        </div>
      </aside>
      <div class="tv-center">
        <h2 class="tv-now" id="tv-now"></h2>
        <div class="player-stage" id="tv-stage" data-status="off">
          <div class="player">
            <div class="tv-layer live"><div class="tv-holder"></div><img class="poster" alt="">
              <h2 class="tv-clip-title"></h2></div>
            <div class="tv-layer" aria-hidden="true"><div class="tv-holder"></div><img class="poster" alt="">
              <h2 class="tv-clip-title"></h2></div>
            <span class="play-badge" aria-hidden="true">▶</span>
            <button type="button" class="player-hit" data-tv="toggle" aria-label="Play clip"></button>
            <div class="subs" id="tv-subs" aria-hidden="true"></div>
          </div>
        </div>
        <div class="player-controls tv-controls">
            <button type="button" class="ctl" data-tv="prev" aria-label="Previous clip" title="Previous clip (↑)"
              disabled>${PREV_ICON}</button>
            <button type="button" class="ctl primary" data-tv="toggle" aria-label="Play clip">▶</button>
            <button type="button" class="ctl" data-tv="next" aria-label="Next clip" title="Next clip (↓)">${NEXT_ICON}</button>
            <span class="time" id="tv-elapsed">0:00</span>
            <input class="progress" id="tv-progress" type="range" min="0" max="100" step="0.1" value="0"
              aria-label="Clip position">
            <span class="time total" id="tv-total">0:00</span>
            <button type="button" class="ctl" data-tv="mute" aria-pressed="false">VOL</button>
            <button type="button" class="ctl" data-tv="fullscreen" aria-label="Full screen" title="Full screen (F)"
              ${document.fullscreenEnabled ? '' : 'hidden'}>⛶</button>
            <a class="ctl yt" id="tv-youtube" href="#" target="_blank" rel="noopener">YouTube ↗</a>
        </div>
      </div>
      <aside class="tv-right">
        <div class="tv-meta" id="tv-meta"></div>
        <output class="tv-countdown" id="tv-countdown" aria-label="Seconds left in this clip"></output>
        <div class="tv-later">
          <div id="tv-next"></div>
          <p class="tv-keys"><kbd>↑</kbd><kbd>↓</kbd> change clip · <kbd>Space</kbd> pause · <kbd>M</kbd> mute ·
            <kbd>F</kbd> full screen</p>
        </div>
      </aside>
    </section>
  </div>`;

  const $ = (id) => document.getElementById(id);
  const root = $('tv');
  const screen = $('tv-screen');
  const stage = $('tv-stage');
  const subtitles = app.subtitles($('tv-subs'));
  const countdown = $('tv-countdown');
  const progress = $('tv-progress');
  const elapsed = $('tv-elapsed');
  const playButton = root.querySelector('.tv-controls .ctl.primary');
  const prevButton = root.querySelector('[data-tv="prev"]');
  const muteButton = root.querySelector('[data-tv="mute"]');
  const rightColumn = root.querySelector('.tv-right');
  const keyHints = root.querySelector('.tv-keys');
  const clipPage = (clip) => `/c/${app.slugOf(clip)}`;

  let items = []; // clips shown (the last HISTORY of them), the one playing at `index`, then the ones queued
  let index = -1;
  let shown = []; // ids of the clips shown, oldest first, for `recent`
  let unplayable = readList(UNPLAYABLE_KEY);
  let refilling = null;
  let powered = false; // playing, or trying to: false before the first clip, and when nothing matches
  let playedAny = false; // a first clip that doesn't start means the browser held it back
  let userMuted = compact; // muted with VOL or M (the home page's TV starts so)
  let autoMuted = false; // muted because the browser won't play sound yet
  let muted = false;
  let status = 'off'; // off | loading | playing | paused | stopped
  let token = 0; // which clip the player is on; events and timers for an older one are ignored
  let finishedFor = 0;
  let countedFor = 0;
  let inRange = false; // the player has reported a time inside the clip (not an ad, not still seeking)
  let scrubbing = false;
  let skips = 0;
  let stallTimer = 0;

  const current = () => items[index];
  // A line under the QR code for what went wrong (or "Picking clips…"). `picking`: about picking clips, so it's
  // cleared once clips are picked.
  let pickingNote = false;
  const note = (text, { picking = false } = {}) => {
    $('tv-status').textContent = text || '';
    pickingNote = picking;
  };

  function setStatus(next) {
    status = next;
    stage.dataset.status = next;
    const label = next === 'playing' ? 'Pause clip' : 'Play clip';
    root.querySelectorAll('[data-tv="toggle"]').forEach((button) => button.setAttribute('aria-label', label));
    playButton.textContent = next === 'playing' ? '❚❚' : '▶';
  }

  function showUnskip() {
    const button = $('tv-unskip');
    button.hidden = !unplayable.length;
    button.textContent = `Try ${unplayable.length} skipped video${unplayable.length === 1 ? '' : 's'} again`;
  }
  showUnskip();

  // --- what's on --------------------------------------------------------------

  // `sliding`: the clip slides in over the one on screen once it's started (slide), and its title and Open clip
  // change then (showNow).
  function renderDetails(item, { sliding = false } = {}) {
    const { clip } = item;
    const { episode } = clip;
    // Who's on it, in SPEAKERS_SHOWN pills at most (the last of them "+3", named in its tooltip), so the right
    // column has room for the countdown and the next clip under them.
    const speakers = clip.speakers || [];
    const shownSpeakers = speakers.length > SPEAKERS_SHOWN ? speakers.slice(0, SPEAKERS_SHOWN - 1) : speakers;
    const moreSpeakers = speakers.slice(shownSpeakers.length);
    const people = shownSpeakers.map((s) => `<li class="${s.verified ? '' : 'unconfirmed'}"
      title="${esc(s.verified ? s.role : `${s.role}, unconfirmed`)}"><span class="avatar" aria-hidden="true">${
      esc(initials(s.name))}</span>${esc(s.name)}${s.verified ? '' : '?'}</li>`).join('') + (moreSpeakers.length
      ? `<li class="tv-people-more" title="${esc(moreSpeakers.map((s) => s.name).join(', '))}">+${moreSpeakers.length}</li>` : '');
    const score = clip.score != null ? Math.round(clip.score * 100) : null;
    // The episode: one line over the screen, or in wide windows the left column's top (tv.css). Who's on it
    // and the clip's score top the right column.
    $('tv-masthead').innerHTML = `
      <img class="tv-mast-poster" src="${esc(clip.links.video_thumbnail)}" alt="">
      <div class="tv-mast-episode"><span class="tv-label">From the episode</span>
        <a href="/r/${esc(clip.run_id)}" data-link>${esc(episode.title)}</a>
        <small>${esc([episode.podcast, day(episode.published_at)].filter(Boolean).join(' · '))}</small></div>`;
    $('tv-meta').innerHTML = `
      ${people ? `<ul class="tv-people" aria-label="On this episode">${people}</ul>` : ''}
      ${score != null ? `<div class="tv-score" role="img" aria-label="Clip score ${score} out of 100">
        <span class="tv-label" aria-hidden="true">Clip score</span><strong aria-hidden="true">${score}</strong><small
        aria-hidden="true">/100</small><span class="tv-meter" style="--score:${score / 100}"></span></div>` : ''}`;
    item.pulls ??= pullQuotes(clip, item.words);
    $('tv-pull').dataset.key = '';
    const url = episodeUrl(clip);
    // The QR code, the next clip and the YouTube link are drawn on the home page's TV too, where they show
    // only in full screen (tv.css .compact), which is the full TV.
    $('tv-qr').innerHTML = `<a href="${esc(url)}" target="_blank" rel="noopener"
      aria-label="Full episode from ${clock(clipStart(clip))} on YouTube">${qrSvg(url)}</a>
      <span>Full episode · ${clock(clipStart(clip))}</span>`;
    subtitles.clear();
    $('tv-total').textContent = clock(clipEnd(clip) - clipStart(clip));
    const youtube = $('tv-youtube');
    youtube.href = clip.links.watch;
    youtube.title = `Watch the full video on YouTube from ${clock(clipStart(clip))}`;
    if (!sliding) showNow(item);
    prevButton.disabled = index <= 0;
    showTime(clipStart(clip), { words: false });
    renderNext();
  }

  // The clip's title above the picture, as its page's link, and Open clip: they change as the clip comes on
  // screen, with the picture. (In full screen the title is on the picture instead: each layer's
  // .tv-clip-title, set as it's readied.)
  function showNow(item) {
    const page = clipPage(item.clip);
    $('tv-now').innerHTML = `<a href="${esc(page)}" data-link>${esc(item.clip.title)}</a>`;
    const open = $('tv-open'); // not on the home page's TV
    if (open) open.href = page;
  }

  function renderNext() {
    const box = $('tv-next');
    const next = items[index + 1];
    box.innerHTML = next
      ? `<button type="button" class="tv-next-card" data-tv="next" aria-label="Next: ${esc(next.clip.title)}. Play now">
          <span class="tv-label">Next</span>
          <img src="${esc(next.clip.thumbnail_url)}" data-fallback="${esc(next.clip.links.video_thumbnail)}" alt="">
          <strong>${esc(next.clip.title)}</strong></button>`
      : `<div class="tv-next-card pending"><span class="tv-label">Next</span><strong>${
        refilling ? 'Picking a clip…' : 'Nothing after this'}</strong></div>`;
    fitKeys();
  }

  // The bar, the seconds left and the transcript at `time` (seconds into the video).
  function showTime(time, { words = true } = {}) {
    const item = current();
    if (!item) return;
    const start = clipStart(item.clip);
    const end = clipEnd(item.clip);
    const length = Math.max(0.001, end - start);
    const into = Math.min(length, Math.max(0, time - start));
    const percent = (into / length) * 100;
    elapsed.textContent = clock(into);
    if (!scrubbing) progress.value = String(percent);
    progress.style.setProperty('--progress', `${percent}%`);
    const left = Math.max(0, Math.ceil(length - into));
    countdown.textContent = `${left}s`;
    countdown.classList.toggle('urgent', status === 'playing' && left > 0 && left <= URGENT_SECONDS);
    if (words) subtitles.show(item.words, time * 1000);
    showPull(item, time * 1000);
  }

  // The quote beside the picture: the one said next waits dimmed, lights up while it's said, then makes way.
  function showPull(item, ms) {
    const box = $('tv-pull');
    const at = pullAt(item.pulls, ms);
    const key = at ? `${item.clip.id}:${at.n}` : '';
    const state = at?.state || '';
    if (box.dataset.key === key && box.dataset.state === state) return;
    const quote = box.querySelector('.tv-quote');
    if (box.dataset.key === key && quote) { // the same quote, now being said or said: just its look changes
      quote.className = `tv-quote ${state}`;
    } else {
      box.innerHTML = at ? `<blockquote class="tv-quote ${state}"><span class="tv-quote-text">${
        esc(item.pulls[at.n].text)}</span></blockquote>${
        item.pulls.length > 1 ? `<span class="tv-pull-dots" aria-hidden="true">${item.pulls.map((_, k) =>
          `<i class="${k === at.n ? 'on' : ''}"></i>`).join('')}</span>` : ''}` : '';
      fitPull();
    }
    box.dataset.key = key;
    box.dataset.state = state;
  }

  // The quote fitted to its column, which in wide windows takes no height of its own (tv.css): as many whole
  // lines as there's room for, with an ellipsis after the last.
  function fitPull() {
    const box = $('tv-pull');
    const text = box.querySelector('.tv-quote-text');
    if (!text) return;
    text.style.removeProperty('--lines');
    const dots = box.querySelector('.tv-pull-dots');
    const need = text.parentElement.offsetHeight
      + (dots ? dots.offsetHeight + parseFloat(getComputedStyle(box).rowGap) : 0);
    const over = need - box.clientHeight; // a pixel's rounding apart when it fits (heights are rounded)
    const line = parseFloat(getComputedStyle(text).lineHeight);
    if (over > 1 && line) text.style.setProperty('--lines', Math.max(1, Math.floor((text.offsetHeight - over) / line)));
  }

  // The keys under the next clip go when the right column, which in wide windows takes no height of its own
  // either, hasn't room for them (many people on the clip and a long next title in a short screen).
  function fitKeys() {
    keyHints.hidden = false;
    keyHints.hidden = rightColumn.scrollHeight > rightColumn.clientHeight + 1;
  }

  // On the TV page, the picture is as tall as the window allows less what's above the TV (tv.css --tv-top):
  // the nav and the toolbar, which wrap onto more lines in some widths.
  function fitScreen() {
    if (compact || document.fullscreenElement === screen) return;
    const top = `${Math.round(screen.getBoundingClientRect().top + app.main.scrollTop + window.scrollY)}px`;
    if (root.style.getPropertyValue('--tv-top') !== top) root.style.setProperty('--tv-top', top);
  }
  // Run in the next frame, not in the observer's callback, where what they resize would be a change made during
  // it (a "ResizeObserver loop" error).
  const fitter = new ResizeObserver(() => requestAnimationFrame(() => {
    if (!root.isConnected) return; // the page was left (it fires as the TV goes)
    fitScreen();
    fitPull();
    fitKeys();
  }));
  for (const el of [root, $('tv-pull'), !compact && document.querySelector('.sidebar')]) if (el) fitter.observe(el);

  // --- the queue --------------------------------------------------------------

  const queued = () => items.slice(index + 1).map((item) => item.clip.id);

  function feedQuery() {
    const query = new URLSearchParams(filters);
    query.set('count', String(BATCH));
    // What's queued counts as shown: the new picks follow it, so they avoid it and its last episode.
    for (const id of [...shown, ...queued()].slice(-RECENT)) query.append('recent', String(id));
    for (const id of unplayable) query.append('exclude_episode_id', String(id));
    return query;
  }

  function refill() {
    if (refilling) return refilling;
    refilling = (async () => {
      const { ok, data } = await app.apiRaw(`/feed?${feedQuery()}`);
      if (!root.isConnected) return 0;
      if (!ok) {
        note(app.problemText(data) || "Couldn't pick clips.", { picking: true });
        return 0;
      }
      items.push(...data.data);
      if (pickingNote) note('');
      return data.data.length;
    })().catch(() => {
      note("Couldn't reach Yolt to pick clips.", { picking: true });
      return 0;
    }).finally(() => {
      refilling = null;
      if (root.isConnected) renderNext();
    });
    renderNext();
    return refilling;
  }

  function trimHistory() {
    if (index <= HISTORY) return;
    const drop = index - HISTORY;
    items.splice(0, drop);
    index -= drop;
  }

  async function next() {
    if (index + 1 >= items.length) await refill();
    if (!root.isConnected) return;
    if (index + 1 >= items.length) {
      stop(items.length ? 'Nothing more to play on this channel.' : 'No clips on this channel yet.');
      return;
    }
    index += 1;
    trimHistory();
    play(1);
    if (items.length - index - 1 <= REFILL_AT) refill();
  }

  function previous() {
    if (index <= 0) return;
    index -= 1;
    play(-1);
  }

  // Back to the start with what the channel's filters pick now. `keep` keeps the clip playing on.
  async function retune({ keep = false } = {}) {
    items = keep && current() ? [current()] : [];
    index = items.length - 1;
    if (!keep) shown = [];
    const added = await refill();
    if (!root.isConnected) return;
    if (!added) {
      if (!items.length) stop(filters.toString() ? 'No clips match this channel.' : 'No clips yet: clip a video first.');
      return;
    }
    if (status === 'off' || status === 'stopped') {
      index += 1;
      if (powered) play(); else renderDetails(current());
    } else {
      await next();
    }
  }

  function stop(text) {
    slideNow({ animate: false });
    clearTimeout(stallTimer);
    token += 1;
    if (live.ready) live.player.pauseVideo();
    setStatus('stopped');
    note(text);
    countdown.classList.remove('urgent');
  }

  // --- the players ------------------------------------------------------------
  // Two decks, each a YouTube player on its own layer of the picture. The live one plays the clip on screen. In
  // its last LEAD seconds the spare starts the next clip muted, under it, from a little before that clip's start,
  // timed to reach its start just as the live one ends; then it slides up over the live one, already playing, and
  // takes the sound. A change asked for earlier (↓, ↑) starts the clip under the one on screen too, which plays on
  // until it's under way. The deck slid away is stopped and becomes the spare.
  //
  // YouTube draws its own controls over every video for its first few seconds, however it's started (the crop
  // hides the title bar and the shelf, not the pause button in the middle). Started LEAD seconds ahead, out of
  // sight, the next clip is past them when it slides in. A change asked for earlier shows them for a moment, as
  // the clip page does.

  const decks = [...stage.querySelectorAll('.tv-layer')].map((layer) => ({
    layer, holder: layer.querySelector('.tv-holder'), poster: layer.querySelector('.poster'),
    title: layer.querySelector('.tv-clip-title'), player: null, ready: false, item: null, preparing: false, failedItem: null,
    lastState: null, loads: 0, created: 0, alignedAt: 0,
  }));
  let [live, spare] = decks;
  let pending = null; // a clip starting under the one on screen: { incoming, outgoing, direction, timer }
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');
  // Whether `deck` has the next clip under way, out of sight.
  const ahead = (deck) => deck.preparing && deck.ready && deck.lastState === STATE.PLAYING;

  function start() {
    if (!current()) return;
    powered = true;
    skips = 0;
    play();
  }

  // Shows the clip at `index`: on the live deck the first time, then on the spare, sliding in from below
  // (`direction` 1, next) or above (-1, previous).
  function play(direction = 1) {
    const item = current();
    if (!item) return;
    token += 1;
    inRange = false;
    clearTimeout(stallTimer);
    shown = [...shown, item.clip.id].slice(-RECENT);
    renderDetails(item, { sliding: powered && Boolean(live.item) });
    if (!powered) return;
    setStatus('loading');
    if (!live.item) { // the first clip: nothing to slide from
      load(live, item);
      return;
    }
    slideNow({ animate: false }); // a change still on its way (↓ pressed twice quickly) lands at once
    const incoming = spare;
    const outgoing = live;
    const ready = incoming.item === item && ahead(incoming);
    live = incoming;
    spare = outgoing;
    outgoing.preparing = false;
    // Started already, in the last seconds of the one before; unless it ran on to its end meanwhile (this one
    // was paused near its end), and then it starts again.
    if (incoming.item === item && incoming.preparing && ![STATE.ENDED, STATE.PAUSED].includes(incoming.lastState)) {
      incoming.preparing = false;
    } else {
      load(incoming, item);
    }
    if (ready) {
      slide(incoming, outgoing, direction);
      stateChanged(incoming, STATE.PLAYING); // it's been playing all along: on screen, it's this clip's start
    } else {
      pending = { incoming, outgoing, direction, timer: setTimeout(() => slideNow(), SLIDE_WAIT_MS) };
    }
    watchStall(token, playedAny ? STALL_MS : FIRST_START_MS);
  }

  // The change on its way lands: the clip started under the one on screen slides in, with its sound.
  function slideNow({ animate = true } = {}) {
    if (!pending) return;
    const { incoming, outgoing, direction, timer } = pending;
    clearTimeout(timer);
    pending = null;
    slide(incoming, outgoing, direction, { animate });
    applyMute();
  }

  function slide(incoming, outgoing, direction, { animate = true } = {}) {
    showNow(incoming.item);
    if (outgoing.ready) outgoing.player.mute();
    incoming.layer.classList.add('live');
    outgoing.layer.classList.remove('live');
    incoming.layer.removeAttribute('aria-hidden'); // its title is the one being read
    outgoing.layer.setAttribute('aria-hidden', 'true');
    // Once out of sight it's stopped, unless it's already starting another clip.
    const rest = () => {
      if (outgoing === spare && !outgoing.preparing && outgoing.ready) outgoing.player.stopVideo();
    };
    if (!animate || reduceMotion.matches) {
      rest();
      return;
    }
    const timing = { duration: SLIDE_MS, easing: 'cubic-bezier(0.2, 0.7, 0.2, 1)' };
    incoming.layer.animate([{ transform: `translateY(${direction * 100}%)` }, { transform: 'none' }], timing);
    outgoing.layer.animate([{ transform: 'none' }, { transform: `translateY(${direction * -100}%)` }], timing)
      .finished.then(rest, rest);
  }

  // In the live clip's last LEAD seconds, the next one starts on the spare, `left` seconds before its start.
  function prepareNext(left) {
    const next = items[index + 1];
    if (!next || spare.item === next || spare.failedItem === next) return;
    load(spare, next, { prepare: true, from: clipStart(next.clip) - left });
  }

  // Keeps the spare where it reaches its clip's start as the live one ends: a pause or a seek moves that.
  function alignSpare(left) {
    if (!ahead(spare) || Date.now() - spare.alignedAt < 1000) return;
    const want = clipStart(spare.item.clip) - left;
    const at = Number(spare.player.getCurrentTime?.());
    if (want >= 0 && Number.isFinite(at) && Math.abs(at - want) > ALIGN_SLACK) {
      spare.player.seekTo(want, true);
      spare.alignedAt = Date.now();
    }
  }

  // Puts `item` on `deck`, muted, from `from` seconds into its video (its start unless said). `prepare`: the
  // spare, starting the next clip ahead of its turn.
  function load(deck, item, { prepare = false, from = clipStart(item.clip) } = {}) {
    deck.item = item;
    deck.preparing = prepare;
    deck.lastState = null;
    deck.alignedAt = 0;
    deck.layer.classList.remove('started'); // its poster covers it until it plays
    deck.poster.src = item.clip.thumbnail_url;
    deck.poster.dataset.fallback = item.clip.links.video_thumbnail;
    deck.title.textContent = item.clip.title; // on its own layer, so it slides in and out with its clip
    const startSeconds = Math.max(0, from);
    if (!deck.player || !deck.ready || deck.loads >= RENEW_EVERY) {
      createPlayer(deck, item, startSeconds);
      return;
    }
    deck.loads += 1;
    deck.player.mute();
    deck.player.loadVideoById({ videoId: item.clip.source.video_id, startSeconds, endSeconds: clipEnd(item.clip) });
  }

  async function createPlayer(deck, item, startSeconds) {
    deck.ready = false;
    deck.player?.destroy?.();
    deck.player = null;
    deck.loads = 1;
    const mine = (deck.created += 1);
    const YT = await app.loadYouTubeApi();
    if (!root.isConnected || mine !== deck.created) return; // replaced meanwhile
    if (!YT) {
      if (deck === live) stop("YouTube's player didn't load. Check the connection, then press ▶.");
      return;
    }
    const box = document.createElement('div');
    deck.holder.replaceChildren(box);
    deck.player = new YT.Player(box, {
      // youtube.com, not youtube-nocookie.com, so it uses the browser's YouTube login (Premium means no ads).
      host: 'https://www.youtube.com',
      videoId: item.clip.source.video_id,
      playerVars: {
        start: Math.floor(startSeconds),
        end: Math.ceil(clipEnd(item.clip)),
        autoplay: 1,
        mute: 1, // the live deck's sound is set once it's on screen (applyMute)
        controls: 0,
        disablekb: 1,
        fs: 0,
        iv_load_policy: 3,
        rel: 0,
        playsinline: 1,
        cc_load_policy: 0,
        origin: window.location.origin,
      },
      events: {
        onReady: () => playerReady(deck),
        onStateChange: ({ data }) => stateChanged(deck, data),
        onError: ({ data }) => failed(deck, data),
      },
    });
  }

  function playerReady(deck) {
    if (!root.isConnected) return;
    deck.ready = true;
    deck.player.mute();
    if (deck === live && !pending) { // the first clip, on screen from the start
      if (!playedAny && !userMuted && !mayPlaySound()) autoMuted = true;
      applyMute();
    }
    deck.player.playVideo();
    if (deck === live) watchStall(token, playedAny ? STALL_MS : FIRST_START_MS);
  }

  // A clip that never starts, or stops to buffer and never carries on, is skipped rather than left on screen.
  // The very first one is different: the browser held it back, so it's tried again muted, which browsers allow.
  function watchStall(mine, ms = STALL_MS) {
    clearTimeout(stallTimer);
    stallTimer = setTimeout(() => {
      if (mine !== token || live.lastState === STATE.PLAYING || status === 'paused' || status === 'stopped') return;
      if (!playedAny) {
        if (!muted) {
          autoMuted = true;
          applyMute();
          if (live.ready) live.player.playVideo();
          watchStall(mine, FIRST_START_MS);
        } else {
          note('Press ▶ to start.');
        }
        return;
      }
      skip("This clip didn't start.");
    }, ms);
  }

  function stateChanged(deck, data) {
    deck.lastState = data;
    if (!root.isConnected) return;
    if (deck !== live) {
      if (deck.preparing && data === STATE.PLAYING) deck.layer.classList.add('started'); // under way, out of sight
      return;
    }
    const item = current();
    if (!item || status === 'off' || status === 'stopped') return;
    const mine = token;
    if (data === STATE.PLAYING) {
      clearTimeout(stallTimer);
      setStatus('playing');
      deck.layer.classList.add('started');
      if (!playedAny && deck.player.isMuted?.() && !muted) autoMuted = true; // YouTube muted itself to start
      playedAny = true;
      skips = 0;
      if (pending?.incoming === deck) slideNow();
      else applyMute();
      if (countedFor !== mine) {
        countedFor = mine;
        // Started ahead of its turn, it may be a little off its start (it was paused or seeked meanwhile).
        const start = clipStart(item.clip);
        const at = Number(deck.player.getCurrentTime?.());
        if (Number.isFinite(at) && Math.abs(at - start) > ALIGN_SLACK) deck.player.seekTo(start, true);
        // Our subtitles are the transcript; YouTube's captions would say it twice (and are cropped off anyway).
        // (Not in the IFrame API's documentation, but what YouTube's own CC button does; app.js uses it too.)
        deck.player.unloadModule?.('captions');
        app.apiRaw('/feed/plays', { method: 'POST', body: JSON.stringify({ clip_id: item.clip.id }) })
          .catch(() => {});
      }
    } else if (data === STATE.ENDED) {
      finish(mine);
    } else if (data === STATE.PAUSED) {
      const time = Number(deck.player.getCurrentTime?.());
      if (inRange && time >= clipEnd(item.clip) - 0.6) finish(mine); // stopped at endSeconds
      else if (status === 'playing') setStatus('paused');
    } else if (status === 'loading' || data === STATE.BUFFERING) {
      watchStall(mine, playedAny ? STALL_MS : FIRST_START_MS); // still busy: buffering, an ad starting
    }
  }

  function failed(deck, code) {
    if (!root.isConnected) return;
    if (deck !== live && !deck.preparing) return; // the one slid away; whatever it was playing is over
    const item = deck.item;
    const blocked = UNPLAYABLE_ERRORS.has(code);
    if (blocked && item && !unplayable.includes(item.clip.episode.id)) {
      unplayable = [...unplayable, item.clip.episode.id];
      localStorage.setItem(UNPLAYABLE_KEY, JSON.stringify(unplayable));
      showUnskip();
      items = items.filter((other, at) => at <= index || other.clip.episode.id !== item.clip.episode.id);
    }
    if (deck !== live) {
      // The next clip, found out early: a video YouTube won't play here is gone from the queue already; any
      // other failure is tried again when its turn comes, without starting it early again.
      deck.failedItem = item;
      deck.item = null;
      deck.preparing = false;
      renderNext();
      if (items.length - index - 1 <= REFILL_AT) refill();
      return;
    }
    if (status === 'off' || status === 'stopped') return;
    skip(blocked ? "YouTube won't play this video here." : "This clip wouldn't play.");
  }

  function skip(reason) {
    skips += 1;
    if (skips >= MAX_SKIPS) {
      stop(`${MAX_SKIPS} clips in a row wouldn't play. Check the connection, then press ▶.`);
      return;
    }
    note(`${reason} Skipped.`);
    next();
  }

  // Once per clip, whichever notices the end first: the tick, or the player stopping at endSeconds.
  function finish(mine) {
    if (mine !== token || finishedFor === mine) return;
    finishedFor = mine;
    next();
  }

  function toggle() {
    if (!powered || status === 'stopped') {
      start();
    } else if (!live.ready) {
      // still loading; the clip starts on its own
    } else if (status === 'playing') {
      live.player.pauseVideo();
      setStatus('paused');
    } else {
      live.player.playVideo(); // paused, or a first clip the browser held back
    }
  }

  // The sound is the live deck's; the spare stays muted.
  function applyMute() {
    muted = userMuted || autoMuted;
    if (live.ready) {
      if (muted) live.player.mute(); else live.player.unMute();
    }
    muteButton.textContent = muted ? 'MUTED' : 'VOL';
    muteButton.setAttribute('aria-pressed', String(muted));
    muteButton.title = autoMuted ? 'Click anywhere for sound' : '';
    $('tv-sound').hidden = !autoMuted;
  }

  function toggleMute() {
    userMuted = !muted;
    autoMuted = false;
    applyMute();
  }

  function toggleFullscreen() {
    if (document.fullscreenElement) document.exitFullscreen?.();
    else screen.requestFullscreen?.().catch(() => {});
  }

  // --- input --------------------------------------------------------------------

  root.addEventListener('click', (event) => {
    const action = event.target.closest('[data-tv]')?.dataset.tv;
    if (!action) return;
    if (action === 'toggle') toggle();
    else if (action === 'next') { if (powered) next(); else start(); }
    else if (action === 'prev') { if (powered) previous(); }
    else if (action === 'mute') toggleMute();
    else if (action === 'fullscreen') toggleFullscreen();
    else if (action === 'reshuffle') retune({ keep: powered && status !== 'stopped' });
    else if (action === 'unskip') {
      unplayable = [];
      localStorage.removeItem(UNPLAYABLE_KEY);
      showUnskip();
      note('Skipped videos are back on.');
    }
  });

  // Leaving for the full video shouldn't leave the clip playing behind it.
  $('tv-youtube')?.addEventListener('click', () => {
    if (status === 'playing') toggle();
  });

  // While dragging, the tick mustn't pull the handle back to the player's time. Dragging seeks without
  // asking YouTube for more video; letting go does.
  progress.addEventListener('pointerdown', () => {
    scrubbing = true;
    window.addEventListener('pointerup', () => { scrubbing = false; }, { once: true });
  });
  const seek = (allowSeekAhead) => {
    const item = current();
    if (!live.ready || !item) return;
    const start = clipStart(item.clip);
    const time = start + (Number(progress.value) / 100) * (clipEnd(item.clip) - start);
    live.player.seekTo(time, allowSeekAhead);
    showTime(time);
  };
  progress.addEventListener('input', () => seek(false));
  progress.addEventListener('change', () => seek(true));

  $('tv-tune')?.addEventListener('submit', (event) => {
    event.preventDefault();
    const form = event.target;
    const q = form.elements.q.value.trim();
    if (q) filters.set('q', q); else filters.delete('q');
    history.replaceState(history.state, '', `/tv${filters.toString() ? `?${filters}` : ''}`);
    retune({ keep: false });
    screen.focus({ preventScroll: true });
  });

  // The first click or key press anywhere lets the browser play sound: a clip muted only for that is unmuted.
  // (Not the VOL button or M, which turn the sound on themselves.)
  const onGesture = (event) => {
    if (!root.isConnected || !autoMuted) return;
    if (event.type === 'keydown' && (event.key === 'm' || event.key === 'M' || isTyping(event.target))) return;
    if (event.target.closest?.('[data-tv="mute"]')) return;
    autoMuted = false;
    applyMute();
  };
  document.addEventListener('pointerdown', onGesture, true);
  document.addEventListener('keydown', onGesture, true);

  const onKey = (event) => {
    if (!root.isConnected || event.metaKey || event.ctrlKey || event.altKey || isTyping(event.target)) return;
    const key = event.key;
    if (key === ' ' && event.target.closest?.('button, a')) return; // Space presses a focused button
    let handled = true;
    if (key === 'ArrowDown' || key === 'PageDown') { if (powered) next(); }
    else if (key === 'ArrowUp' || key === 'PageUp') { if (powered) previous(); }
    else if (key === ' ' || key === 'k' || key === 'K') toggle();
    else if (key === 'm' || key === 'M') toggleMute();
    else if (key === 'f' || key === 'F') toggleFullscreen();
    else handled = false;
    if (handled) event.preventDefault();
  };
  const keys = compact ? root : document;
  keys.addEventListener('keydown', onKey);
  const onFullscreen = () => screen.classList.toggle('is-full', document.fullscreenElement === screen);
  document.addEventListener('fullscreenchange', onFullscreen);

  // Keeps playback inside the clip and drives the bar, countdown and transcript. Stops, with everything it
  // started, once the page is replaced.
  const timer = setInterval(() => {
    if (!root.isConnected) {
      clearInterval(timer);
      clearTimeout(stallTimer);
      clearTimeout(pending?.timer);
      fitter.disconnect();
      keys.removeEventListener('keydown', onKey);
      document.removeEventListener('pointerdown', onGesture, true);
      document.removeEventListener('keydown', onGesture, true);
      document.removeEventListener('fullscreenchange', onFullscreen);
      if (document.fullscreenElement === screen) document.exitFullscreen?.();
      for (const deck of decks) {
        deck.created += 1; // a player still on its way isn't made
        deck.player?.destroy?.();
        deck.player = null;
      }
      return;
    }
    if (!live.ready || status !== 'playing') return;
    const item = current();
    const time = Number(live.player.getCurrentTime?.());
    if (!item || !Number.isFinite(time)) return;
    const start = clipStart(item.clip);
    const end = clipEnd(item.clip);
    // Until the player reports a time inside the clip, it's still seeking there (or showing an ad).
    if (time >= start - 0.5 && time <= end) inRange = true;
    if (!inRange) return;
    if (end - time <= LEAD) {
      prepareNext(end - time);
      alignSpare(end - time);
    }
    if (time >= end - END_SLACK) finish(token);
    else if (time < start - 1) live.player.seekTo(start, true);
    else showTime(time);
  }, 150);

  // --- start ----------------------------------------------------------------------

  setStatus('off');
  note('Picking clips…', { picking: true });
  await retune();
  if (!root.isConnected || !current()) return;
  start();
  if (!compact) screen.focus({ preventScroll: true });
}
