# Security model

## Who can call it

One shared API key, checked by middleware **before any MCP method runs** (so `initialize` and `tools/list` need it
too). `Authorization: Bearer <key>` or `X-Api-Key: <key>`. Compared in constant time on SHA-256 digests (length does
not leak). The server refuses to start without a key of at least 16 characters unless
`CALDAV_MCP_ALLOW_NO_AUTH=true`. `GET /health` is the only exempt route and answers `ok`.

Failed attempts are rate-limited per client address with growing back-off, in memory, bounded in size. The address is
the TCP peer. `X-Forwarded-For` is ignored unless `CALDAV_MCP_TRUST_PROXY=true`, and then only its rightmost entry
(written by the proxy) is used; otherwise a caller could choose its own bucket, or lock everyone else out of theirs.

The key is never logged. Neither are credentials: the password, username and key are removed from the environment
after startup, and `Config.__repr__` omits them.

## What the agent can do

* `CALDAV_ALLOW_TOOLS` / `CALDAV_DENY_TOOLS` / `CALDAV_MCP_READ_ONLY` decide which tools are *registered*. A tool that
  is not registered cannot be called and is not listed. Move and attendee tools can each be removed alone.
* Credentials never reach the agent. Error messages name an HTTP status and a remedy, not the URL, and server-supplied
  error text is quoted, labelled and defused.
* Writes are validated and built with the icalendar library, which escapes on serialisation. Control characters are
  refused, not stripped. Values are never concatenated into iCalendar text or XML; CalDAV queries are built with lxml,
  which escapes (a test sends `]]><x/>` and friends).

## Event text is untrusted

Titles, locations, descriptions, categories, calendar names and attendee names are written by whoever created or
shared the event, and an invitation from a stranger arrives with no human reading it. This server:

1. removes zero-width, bidirectional-override and Unicode tag characters, and control characters;
2. normalises (NFKC) before defusing markdown, so full-width brackets cannot smuggle an image; `![` never survives,
   which removes the auto-fetch exfiltration channel;
3. makes titles and locations single lines and caps every field (400 / 2,000 characters);
4. reports injection-shaped text in `flags` rather than hiding or deleting it (a signal, not a blocklist) - patterns
   such as instruction-override, role-injection, credential-request, `calendar-command`, `meeting-coercion` - each
   tested to be linear-time on hostile input;
5. marks every answer `untrusted: true, source: "caldav"` and, for a single event, fences the text with a random
   nonce and a datamark on every line, with a reminder after the block;
6. states in the server instructions that all of it is data.

None of this is a guarantee, and it says so. What limits the damage is the rest of the design: scoped writes, tool
allow-lists, conditional writes, no tool that fetches a URL taken from an event, and no tool that returns
credentials.

## Transport to the CalDAV server

TLS verification is on by default and can only be disabled by an exact, explicit value; a typo stops the server
instead of disabling it. Plain `http://` to a non-loopback host is refused unless explicitly allowed.
