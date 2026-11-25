# hermes-consult (enckequity fork)

A website question-and-answer widget with a contact form. Visitors ask by text (voice is
optional) and get answers drawn only from the facts the business published. Contact requests
become cards on the workspace's kanban board.

This fork of [dandacompany/hermes-consult](https://github.com/dandacompany/hermes-consult)
(MIT, based on `db65cc68`) is for hosted, headless workspaces:

- **No account-login voice lane.** The voice lane that used a chat subscription login is gone.
  Voice only uses the business's own key (`HERMES_CONSULT_OPENAI_API_KEY`, never the generic
  `OPENAI_API_KEY`), and only when `voice_enabled` is true. It is off by default.
- **Config-driven.** No Desktop panel, dashboard routes or web admin. `hermes consult serve`
  follows plugin settings and a sites file and reports in a status file, so a portal can drive it.
- **Leads go to kanban.** Each contact request becomes one card, parked as `blocked` for a
  person, so no agent is ever dispatched on visitor text. A CRM destination can be added later.
- **Caps are always on.** Every per-session and daily cap has a default and none can be
  turned off. Two caps are new: questions per site per day and contact requests per site per day.
- **Neutral text.** Visitor-facing text names no engine or vendor.
- **Loopback only.** The server listens on 127.0.0.1. Control and connector requests that
  arrive through a proxy are refused.

## Registers

One CLI command, `hermes consult` (`serve`, `status`). No agent tools, hooks, middleware,
dashboard routes or toolset.

## Configure

Plugin settings (`plugins.entries.hermes-consult.settings` in config.yaml):

| Setting | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Run the consultation server. While false, `serve` only writes status. |
| `public_origin` | (none) | The HTTPS origin visitors' browsers reach, for example `https://consult.example.com`. |
| `port` | `4318` | Loopback port of the server. |
| `proxy_hops` | `0` | Proxies in front of the server whose `X-Forwarded-For` is trusted for per-visitor caps. |
| `voice_enabled` | `false` | Allow voice for sites with `"engine": "api"`, using the business's own key. |
| `lead_handoff` | `kanban` | `kanban` files each lead as a card; `none` keeps leads in the local database only. |
| `kanban_board` | current board | Board slug for lead cards. |

Sites, `<HERMES_HOME>/consult/sites.json`:

```json
{"sites": [{
  "key": "main",
  "name": "Acme Plumbing",
  "origins": ["https://www.acme.example", "https://acme.example"],
  "greeting": "Hi, how can we help?",
  "knowledge": "Opening hours: ...",
  "rules": "Never quote prices; offer a callback.",
  "dailyQuestions": 200
}]}
```

Site fields and caps (all optional except `key`, `name`, `origins`):

| Field | Default | Range |
| --- | --- | --- |
| `engine` | `text` | `text` or `api` (voice) |
| `language` | `auto` | BCP 47 tag or `auto` |
| `maxSeconds` (per session) | 180 | 30 to 600 |
| `maxConcurrent` | 1 | 1 to 5 |
| `dailySeconds` (per site) | 1800 | 60 to 86400 |
| `dailyStarts` (per visitor and per IP) | 3 | 1 to 100 |
| `cooldownSeconds` | 60 | 0 to 3600 |
| `sessionQuestions` | 30 | 1 to 60 |
| `dailyQuestions` (per site) | 200 | 1 to 2000 |
| `dailyLeads` (per site) | 50 | 1 to 500 |
| `enabled` | `true` | `false` pauses the site |
| `turnstileSiteKey` | (none) | optional bot check |

Unknown fields are refused. The service applies the file when it changes. A removed site is
paused, not deleted. `<HERMES_HOME>/consult/status.json` lists each site's public id, embed
snippet, whether it is online and any error.

Each answer is one tool-free `ctx.llm.complete` call (800 tokens at most) on the workspace's model.

## Public origin

The server binds 127.0.0.1 only. To serve visitors, put an HTTPS reverse proxy in front of it
at `public_origin` that:

- terminates TLS for that hostname and forwards to `127.0.0.1:<port>`;
- forwards only `GET /widget.js`, `/frame.js`, `/frame.css`, `/i18n.js`, `/voice-protocol.js`,
  `/frame/<site>`, `/health`, and `GET`/`POST`/`OPTIONS` under `/public/`;
- does not forward `/api/` or `/connector/` (the server refuses them anyway when they carry
  `X-Forwarded-For`, `Forwarded` or `Via`);
- adds `X-Forwarded-For`. Set `proxy_hops` to the number of proxies in front, otherwise all
  visitors share one per-IP allowance;
- adds edge rate limits.

The embedding website's CSP must allow the origin for `script-src`, `frame-src` and
`connect-src`. For voice, its `Permissions-Policy` must allow the microphone for that origin.

## Develop

```sh
npm ci && npm run build          # rebuilds dist/server.mjs
npm run typecheck && npm test    # Node tests
python -m unittest discover -s tests -p 'test_*.py'   # with the engine's Python
```

Requires Node.js 24 or later (`node:sqlite`).

## License

[MIT](./LICENSE) © 2026 Dante Labs. Fork changes MIT. See [NOTICE.md](./NOTICE.md) and
[THIRD_PARTY_NOTICES.txt](./THIRD_PARTY_NOTICES.txt).
