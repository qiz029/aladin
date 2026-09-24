# Public browser origin — 2026-09-23

User reported that navigation, including My Collection, sometimes reached a Tailscale URL.
Current public links use relative paths; an authenticated browser visit to `/gallery`
stayed on the public domain. The reported public-to-tailnet transition was not reproduced.
A separate proxy issue was reproduced: `/gallery/` and other slash variants generated
absolute HTTP redirects because TLS terminates at Cloudflare.

## Change

- `ALADIN_PUBLIC_URL` configures the canonical browser origin. Compose defaults it to
  `https://aladin.example.com`; explicitly empty disables it for local development.
- Browser routes (`/`, `/apps`, `/jobs`, `/gallery`, `/docs`, `/redoc`, `/openapi.json`,
  `/static`, including children) on an alternate host return 307 to the public origin,
  preserving path/query and method. Old tailnet bookmarks now converge on the same UI.
- Same-host absolute redirects from the framework become relative redirects, retaining
  the browser's HTTPS origin across Cloudflare -> HTTP origin forwarding.
- Canonical comparison uses Host, not the internal HTTP scheme, preventing proxy loops.
- `/api/v1` stays directly available to agents inside the tailnet. External redirects are
  preserved. No Cloudflare Access/tunnel configuration or GPU worker changes required.

## Validation

- Eight focused tests: hostname/IP aliases, query preservation, no proxy loop, slash
  redirect, private API, form redirects, local-development opt-out, external redirects.
- Deploy only `api` with `--no-deps`; an active production generation task was observed
  before deployment, so worker and database must not be recreated.
- Full suite: 106 tests passed.
- Live origin checks passed: tailnet `/gallery?kind=video` -> public HTTPS with query;
  tailnet `/apps/image` -> public HTTPS; private `/api/v1/params` still returns 200;
  public Host `/gallery` returns 200 without a loop; `/gallery/` and `/docs/` return
  relative slash-correction redirects.
- Authenticated Chrome: opening the old tailnet `/apps/director` URL ended at
  `https://aladin.example.com/apps/director`; clicking My Collection ended at
  `https://aladin.example.com/gallery`. Only the API container was recreated.
