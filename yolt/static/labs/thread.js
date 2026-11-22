// Thread: an idea in, an ordered reel of clips out (GET /labs/thread). Plays through in order, and saves
// as a compilation. Removing a clip asks POST /segments/review about what's left.

const LENGTHS = [4, 6, 8, 10, 12];

export default {
  async page(params, view, ctx) {
    const { esc, main, stillOn } = ctx;
    const q = params.get('q') || '';
    const limit = LENGTHS.includes(Number(params.get('limit'))) ? Number(params.get('limit')) : 6;
    const unused = params.get('unused') === '1';
    main.innerHTML = `${ctx.heading()}
      <p class="meta-line">Type an idea. You get clips on it that each lead into the next, ready to play through
        and save as a compilation.</p>
      <form class="search-form" id="thread-form">
        <input type="search" name="q" value="${esc(q)}" maxlength="300" placeholder="An idea, e.g. fear of AI taking jobs"
          aria-label="Idea" required>
        <select name="limit" aria-label="How many clips">${LENGTHS.map((n) =>
    `<option value="${n}" ${n === limit ? 'selected' : ''}>${n} clips</option>`).join('')}</select>
        <label class="check"><input type="checkbox" name="unused" ${unused ? 'checked' : ''}> Never used</label>
        <button type="submit" class="box-button small">Thread it</button>
      </form>
      <div id="thread-result"></div>`;
    main.querySelector('#thread-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const next = new URLSearchParams({ q: form.elements.q.value.trim() });
      if (Number(form.elements.limit.value) !== 6) next.set('limit', form.elements.limit.value);
      if (form.elements.unused.checked) next.set('unused', '1');
      ctx.navigate(`/labs/thread?${next}`);
    });
    if (!q) return;
    const box = main.querySelector('#thread-result');
    box.innerHTML = '<p class="meta-line">Threading…</p>';
    const query = new URLSearchParams({ q, limit: String(limit) });
    if (unused) query.set('usage', 'never');
    const { ok, status, data } = await ctx.apiRaw(`/labs/thread?${query}`);
    if (!stillOn(view, 'lab')) return;
    if (!ok) {
      box.innerHTML = `<p class="error-text">${esc(ctx.problemText(data) || `Thread failed (${status}).`)}</p>`;
      return;
    }
    if (!data.clips.length) {
      box.innerHTML = '<p class="meta-line">No clips match that idea.</p>';
      return;
    }
    showThread(box, data, view, ctx);
  },
};

function showThread(box, thread, view, ctx) {
  const { esc, seconds } = ctx;
  let clips = thread.clips;
  let hops = thread.hops; // hops[i]: clips[i] to clips[i + 1]; null once a removal joins two clips
  let review = thread.review;
  let playing = 0;

  const render = () => {
    const episodes = new Set(clips.map((c) => c.episode.id)).size;
    box.innerHTML = `
      <p class="meta-line">${clips.length} clips · ${seconds(clips.reduce((n, c) => n + c.duration_ms, 0) / 1000)} ·
        ${episodes} episode${episodes === 1 ? '' : 's'}</p>
      <div class="thread-layout">
        <div class="thread-player">
          ${ctx.playerMarkup(clips[playing])}
          <p class="meta-line thread-now">Now: ${playing + 1} of ${clips.length} · ${esc(clips[playing].title)}</p>
          <div class="actions">
            <button type="button" class="box-button small" data-play-from="0">Play from the start</button>
          </div>
          ${findings(review, esc)}
          ${ctx.compilationForm(thread.q)}
        </div>
        <ol class="thread-list">${clips.map((clip, i) => `
          <li class="${i === playing ? 'current' : ''}">
            <button type="button" class="thread-item" data-play-from="${i}" aria-label="Play ${esc(clip.title)}">
              <span class="n">${i + 1}</span>
              <span class="thread-text"><span class="title">${esc(clip.title)}</span>
                <span class="from">${esc(clip.episode.title || '')} · ${seconds(clip.duration_ms / 1000)}</span></span>
            </button>
            <a class="link-button" href="/c/${esc(ctx.slugOf(clip))}" data-link>open</a>
            <button type="button" class="link-button" data-remove="${i}" ${clips.length <= 1 ? 'disabled' : ''}
              aria-label="Remove ${esc(clip.title)}">remove</button>
            ${i < clips.length - 1 ? `<span class="hop">${hops[i] == null ? '↓'
    : `↓ ${Math.round(hops[i] * 100)}% alike`}</span>` : ''}
          </li>`).join('')}
        </ol>
      </div>`;
    ctx.bindCompilationForm(box.querySelector('.lab-save'), () => clips.map((c) => c.id));
    ctx.mountPlayer(clips[playing], { onComplete: next });
  };

  // Each clip's end starts the next; a newer pick (or leaving the page) stops the old one handing on.
  const play = (i, autoplay) => {
    playing = i;
    const stage = box.querySelector('.player-stage');
    if (!stage) return;
    stage.outerHTML = ctx.playerMarkup(clips[i]);
    box.querySelectorAll('.thread-list li').forEach((li, n) => li.classList.toggle('current', n === i));
    box.querySelector('.thread-now').textContent = `Now: ${i + 1} of ${clips.length} · ${clips[i].title}`;
    ctx.mountPlayer(clips[i], { autoplay, onComplete: next });
  };
  function next() {
    const from = playing;
    if (ctx.stillOn(view, 'lab') && box.isConnected && from + 1 < clips.length) play(from + 1, true);
  }

  box.addEventListener('click', async (event) => {
    const start = event.target.closest('[data-play-from]');
    if (start) {
      play(Number(start.dataset.playFrom), true);
      return;
    }
    const remove = event.target.closest('[data-remove]');
    if (!remove || clips.length <= 1) return;
    const i = Number(remove.dataset.remove);
    clips = clips.filter((_, n) => n !== i);
    hops = [...hops.slice(0, Math.max(0, i - 1)), ...(i > 0 && i < hops.length ? [null] : []), ...hops.slice(i + 1)];
    playing = Math.min(playing > i ? playing - 1 : playing, clips.length - 1);
    const body = { segments: clips.map((c) => ({ clip_id: c.id })) };
    const { ok, data } = await ctx.apiRaw('/segments/review', { method: 'POST', body: JSON.stringify(body) });
    if (ok) review = data;
    if (box.isConnected) render();
  });
  render();
}

function findings(review, esc) {
  const warnings = review.findings.filter((f) => f.severity === 'warning');
  if (!warnings.length) return '<p class="meta-line">Review: nothing to flag.</p>';
  return `<details class="thread-review"><summary>Review: ${warnings.length} thing${warnings.length === 1 ? '' : 's'} to check</summary>
    <ul>${warnings.map((f) => `<li>${f.positions.length ? `#${f.positions.join(', #')}: ` : ''}${esc(f.message)}</li>`).join('')}</ul>
  </details>`;
}
