// For you: on the home page, clips like the one saved last in this browser: "Because you saved …"
// (GET /clips?like=…). Used or not doesn't matter, and a saved clip never shows. When the last saved clip has
// no vector yet (a 422) or nothing like it, the one saved before it stands in, and so on, up to SPARE_SEEDS.
// Quiet when nothing is saved, or none of the saved clips is in this database or embedded yet.

const PER_SEED = 5; // as many as the home page's other columns
const SPARE_SEEDS = 2; // saved clips tried in turn after the last one
const MAX_EXCLUDE = 200; // GET /clips takes up to 200 `exclude_id`

export default {
  async home(view, ctx) {
    const { esc } = ctx;
    const box = document.getElementById('labs-home');
    const saved = ctx.savedClips().slice(0, MAX_EXCLUDE); // newest first
    if (!box || !saved.length) return;
    // Keep the saved clips this database has: an ID from another database comes with a different slug.
    const ids = new URLSearchParams([...saved.map((s) => ['id', String(s.id)]), ['view', 'compact']]); // all of them
    const known = new Map((await ctx.api(`/clips?${ids}`).then((page) => page.data).catch(() => [])).map((c) => [c.id, c]));
    const mine = saved.map((s) => known.get(s.id)).filter((c, i) => c && ctx.slugOf(c) === saved[i].slug);
    if (!mine.length) return;
    const exclude = mine.map((c) => ['exclude_id', String(c.id)]);
    let seed = null;
    let picks = [];
    for (const candidate of mine.slice(0, 1 + SPARE_SEEDS)) {
      const query = new URLSearchParams([['like', String(candidate.id)], ...exclude, ['diversify', 'true'],
        ['limit', String(PER_SEED)], ['view', 'compact']]);
      const { ok, data } = await ctx.apiRaw(`/clips?${query}`);
      if (ok && data.data.length) {
        seed = candidate;
        picks = data.data;
        break;
      }
    }
    if (!seed || ctx.state.view !== view || !box.isConnected) return;
    // The list only: the home page puts it in its For you column (app.css .home-picks).
    box.innerHTML = `<section class="home-col">
      <h2 class="label home-because"><span>Because you saved <a href="/c/${esc(ctx.slugOf(seed))}" data-link>${
  esc(seed.title)}</a> ${ctx.badge}</span></h2>
      <ul class="home-list">${picks.map(ctx.homeClip).join('')}</ul></section>`;
  },
};
