# Shockwave docs

Static, no-build documentation site for the Shockwave Discord bot.

```
index.html            Home / overview / quick-start flow
add-to-server.html     OAuth invite walkthrough + permissions reference
commands.html          Full slash command reference
self-hosting.html      Setup guide for running your own instance
assets/styles.css      Design system (colors pulled from the crest artwork)
assets/main.js         Nav toggle + active link highlighting + embed animation
assets/img/            Logo mark, full crest, favicon
netlify.toml           Netlify config (clean URLs + headers)
```

## Deploy to Netlify

**Option A — drag and drop**
1. Go to [app.netlify.com/drop](https://app.netlify.com/drop).
2. Drag this whole folder onto the page.
3. Done — Netlify gives you a live URL immediately.

**Option B — Git-connected (recommended for updates)**
1. Push this folder to a GitHub/GitLab/Bitbucket repo.
2. In Netlify: **Add new site → Import an existing project**, pick the repo.
3. Build settings: leave **Build command** empty and set **Publish directory** to `.`
   (already set in `netlify.toml`, so Netlify should pick it up automatically).
4. Deploy. Every push to the connected branch redeploys automatically.

**Option C — Netlify CLI**
```bash
npm install -g netlify-cli
cd this-folder
netlify deploy --prod
```

No environment variables, no build step, no server-side code — it's three
HTML files, one stylesheet, and one small script.

## Editing content

Each page repeats the sidebar nav markup rather than sharing a template, so
there's no build step required — just edit the `<nav>` block in each HTML
file if you add or rename a page. Command reference entries live in
`commands.html` as `.cmd-card` blocks; copy an existing one as a starting
point for a new command.
