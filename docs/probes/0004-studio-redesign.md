# Studio UI redesign — 2026-09-23

Deployed to workstation at http://<workstation-ip>:8765.

## Scope

Reworked all nine human-facing Jinja templates: studio overview, director, image,
edit, video, history, job detail, and collection, with a shared navigation shell.
Warm neutral surfaces, lavender accents, larger media previews, responsive layouts,
collapsible parameters, and clear creation actions replace the prior dark forms.

Shared assets live in `aladin/static/`; `web.py` only adds the `/static` mount.
No API, validation, queue, model, database, or worker behavior changed. All original
creation form fields and actions remain. `studio.js` progressively enhances the
same form endpoints with submission feedback and inline server errors, upload
previews, and an accessible media dialog. Ordinary HTML submission remains the
fallback. Dialog close, focus restoration, and background scroll handling work.

When changing shared CSS/JS, bump the asset query version in `base.html` so existing
browser caches cannot retain an older layout.

## Validation

- 99 existing regression tests passed; one page-copy assertion changed from
  “最近产物” to “最近生成”.
- Before/after OpenAPI JSON is identical across all 39 paths.
- Compared all creation form control names against the pre-redesign templates.
- Chrome tested at desktop 1440 px, tablet 768 px, and mobile 320/390/430 px.
  No horizontal page overflow was observed. Refined the narrow hero layout after
  visually checking it at 430 px.
- Isolated local preview database, with copies of the previous benign coffee-shop
  image and brass-object video smoke artifacts. No generation worker ran there.
- Browser interactions verified: example insertion, image submission and redirect,
  duplicate-submission inline error and retry state, upload preview, edit-mode
  switching, image enlargement/close, collection promotion, parameter reuse,
  and playback of the 2.063-second video.
- `node --check aladin/static/studio.js` passed.
- Deployment rebuilt API/worker and kept Postgres running. Runtime import smoke passed.
- After deployment, all nine checked live URLs returned 200 and the new shell.
  All three static asset bytes match local source; known image and video bytes
  match their recorded SHA-256. Live director page was visually inspected in Chrome.

Local evidence: `.cache/redesign-tests.log`, `.cache/redesign-deploy.log`,
`.cache/redesign-live-verification.json`. Pre-change templates and web module are
in `.cache/redesign-before/`. Existing production jobs and collections are retained.

This is UI and compatibility validation. It does not claim a new GPU generation
run or user acceptance of the visual direction.
