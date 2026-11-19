# Vendored MCP server

**Source:** `github.com/ShaguftaMethwani/mcpserver-test` (private), branch `main`, commit
`4677e619bf1d9c460f96c19f7a16038c2333922d` (7 September 2026). Copied without its git history
on 18 November 2026. Upstream is unchanged; every change below exists only in this copy.

The Support Desk runs this server over **stdio** as a subprocess of the backend (`src/index.ts`,
built to `dist/index.js`). The backend's gate decides whether a write may happen; this server
only executes the call it is given.

## Changes from upstream

1. **Send tool removed.** `gmail_send_email`, its handler, schema, service method, types and
   test are deleted, and the `gmail.send` OAuth scope is no longer requested. The server lists
   exactly two tools: `gmail_create_draft` and `google_docs_append`.
   `gmail.compose` still allows sending through the Gmail API, so the scope alone would not
   prevent it. What prevents it is that no tool can send, plus the backend's allow-list.
2. **Draft recipient optional.** `gmail_create_draft` accepts no `to` (the `To:` header is
   left out). The Support Desk never stores customer email addresses, so its drafts are
   addressed by the person who sends them from Gmail.
3. **Append returns the revision.** `google_docs_append` returns `revision_id` (the document
   revision after the write) and `skipped`, so the gate can record what was written.
4. **Idempotent append.** Optional `dedupe_key`: it must appear in `content`; if the document
   already contains it, nothing is appended and `skipped` is `true`. The write is pinned to
   the revision that was read (`writeControl.requiredRevisionId`), so a change in between
   makes the call fail and be retried rather than write twice or at a stale position. The
   backend passes the pending action id as the key.
5. **Logs to stderr.** The logger wrote to stdout (pino's default), which is the MCP protocol
   channel over stdio: a log line there was read by the client as a malformed message. It now
   writes to stderr, as the entry point's comments already said it did.
6. **Only the Docs and Gmail clients.** The server imported `googleapis`, which loads every
   Google API: 147 MB and half a second on a laptop, and over 30 s at Render's 0.1 CPU, where
   the backend's tool call timed out. It now uses `@googleapis/docs`, `@googleapis/gmail` and
   `google-auth-library` (99 MB for the whole server). Their versions are the ones on
   `googleapis-common` 7, the same `google-auth-library` 9 as before. `googleapis` is a dev
   dependency, used only by `npm run smoke` (which also lists Drive files).

Tests were updated to match: the send tests are removed; new tests cover drafts without a
recipient, the send API never being called, revision ids, deduplication and the pinned write,
and an in-memory MCP client that checks the server lists exactly the two tools and refuses a
call to `gmail_send_email`.

## Not used by The Support Desk

- `src/server-http.ts` (the HTTP transport) has **no authentication**. Do not deploy it as is.
- `railway.json` and `docs/` describe the original standalone project; `docs/` still mentions
  the removed send tool.

## Credentials

`GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` from the environment, and the OAuth tokens either
from `tokens.json` (local) or `GOOGLE_TOKENS_JSON` (server). Create the token with only the
`gmail.compose` and `documents` scopes (`npm run auth`).
