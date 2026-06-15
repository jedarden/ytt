# MCP Tool Response Limits — Research for `ytt` (YouTube transcript MCP server)

> **Scope:** Does anything limit the size/length of what an MCP tool returns, especially for a *remote* MCP server used as a Claude custom connector? `ytt`'s single tool returns YouTube transcripts, which can be tens of thousands of tokens / hundreds of KB. This document gathers current (2026) authoritative findings and a recommended design.

---

## Summary (TL;DR)

1. **The MCP specification itself defines NO maximum size** (bytes or tokens) for a tool call result (`CallToolResult`). This is explicit — a community proposal to add one ([Discussion #2211](https://github.com/modelcontextprotocol/modelcontextprotocol/discussions/2211)) was met with maintainer skepticism and **has not been adopted**. Treat "no spec limit" as a fact, not an omission in this research.
2. **The limit is imposed CLIENT-side, and it varies by client.** The hard numbers that matter for `ytt`:
   - **Claude Code:** soft warning at **10,000 tokens**, configurable via `MAX_MCP_OUTPUT_TOKENS` (commonly cited default **25,000 tokens**), with a **hard ceiling of 500,000 characters**.
   - **Anthropic API MCP connector (`mcp-client-2025-11-20`):** no *publicly documented* per-tool-result token cap. The whole tool result is injected into the model's context, so the real ceiling is the **model context window** (1M tokens on Opus 4.x) and your request's `max_tokens` budget — not a separate MCP cap. **Assume it counts fully against context and is billed as input tokens.**
   - **Claude apps (web / Desktop / mobile):** large results are accepted but **the terminal/UI display may be truncated** (a documented Claude Code display bug truncated to ~700 chars); display truncation ≠ the model losing data, but it is a real UX wrinkle.
3. **Pagination in MCP applies ONLY to list operations** (`tools/list`, `resources/list`, `resources/templates/list`, `prompts/list`) — **NOT to `tools/call` results.** A tool cannot return a `nextCursor` to page its own output. Anyone assuming "tool results paginate" is wrong.
4. **Tool results are content blocks**: `text`, `image`, `audio`, `resource_link`, and embedded `resource`. A **`resource_link`** lets you return a small URI instead of inlining the whole transcript — this is the single most important lever for `ytt`.
5. **Timeouts are real and client-set** (commonly **60 s** default). A long Whisper transcription can blow past this unless the tool emits `notifications/progress` AND the client honors `resetTimeoutOnProgress` (not universally on).
6. **Recommended pattern for `ytt`:** return the transcript inline when it's comfortably under a conservative budget (~**8–10K tokens**); for longer transcripts, either (a) **chunk** with a `start`/`cursor`-style argument on the tool plus metadata telling the model how to fetch the next chunk, or (b) return a **`resource_link`/URL** the model fetches on demand, and/or **offer server-side summarization**. Do **not** rely on the client to gracefully truncate — design for the limit.

---

## 1. Is there a documented maximum tool-result size in the MCP spec?

**No.** The MCP specification (2025-06-18 server/tools) describes the `tools/call` response shape — a `content` array plus optional `structuredContent` and `isError` — but **specifies no maximum length, byte size, or token count** for it.

The gap is acknowledged in the community. [Discussion #2211 "Response size limit for MCP responses to prevent context overflow"](https://github.com/modelcontextprotocol/modelcontextprotocol/discussions/2211) proposed a configurable `max_response_bytes` (suggested defaults **256 KB–512 KB**, per-tool/per-server overrides). Maintainer response was **skeptical**, not endorsing:

> "clients can also truncate or summarise large responses too… I don't think we need to make this limit" — SamMorrowDrums (collaborator)

The thread's conclusion: **no limit is currently specified or mandated in MCP**, and the favored solution is at the implementation/client level (e.g., clients store large outputs in temp files and let the model fetch them).

**Safe practical assumption:** the protocol will not stop you from returning a 500 KB transcript, but **something downstream will** — so size for the client, not the protocol.

---

## 2. Client-side limits imposed by Claude (concrete numbers)

The protocol is permissive; the *clients* are where transcripts actually get capped. Numbers found:

### Claude Code (CLI)
- **Soft warning at 10,000 tokens** of MCP tool output. ([Claude Code MCP docs](https://code.claude.com/docs/en/mcp))
- **`MAX_MCP_OUTPUT_TOKENS`** environment variable raises the threshold; the **commonly cited default is 25,000 tokens** (also surfaced as a general MCP-implementation default). Set e.g. `MAX_MCP_OUTPUT_TOKENS=50000`.
- **Hard ceiling: 500,000 characters** per tool result in Claude Code, regardless of the token setting.
- A separate **display** truncation bug ([claude-code#2638](https://github.com/anthropics/claude-code/issues/2638)) showed terminal output cut to **~700 characters** (an 8,046-char response rendered as ~700 + "…"), with the full body still saved to a temp file. There was also an associated `ERR_CHILD_PROCESS_STDIO_MAXBUFFER` (Node `maxBuffer`) error for very large stdio payloads. **This affects what the *user sees*, not necessarily what the *model receives*** — but it's a real surface wrinkle for big transcripts.

### Anthropic API MCP connector (`mcp_servers` / `mcp_toolset`, beta `mcp-client-2025-11-20`)
- The [MCP connector docs](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector) document the request/response shapes (`mcp_tool_use` / `mcp_tool_result` blocks) but **do NOT document any per-tool-result size or token cap.**
- The tool result is returned as an `mcp_tool_result` content block and **placed into the model's context window**, so the effective limits are:
  - the **model context window** (1M tokens for Opus 4.6/4.7/4.8, Sonnet 4.6; 200K for Haiku 4.5), shared with everything else in the request; and
  - your request's overall token budget. Tool results are **billed as input tokens** and are **not covered by Zero Data Retention** on this connector.
- **No documented automatic truncation** on this path. If the transcript + conversation exceeds the context window you get a context-overflow error / degraded behavior, not a silent clip. **Safe assumption: the full result counts against context and costs input tokens.**

> Note — related but distinct: Anthropic's **Managed Agents (CMA)** runtime auto-offloads MCP tool outputs **larger than 100K tokens** to a file in the sandbox, handing the model a truncated preview + file path to `read`. That 100K figure is **CMA-specific** and does **not** apply to the API MCP connector or the consumer apps. Don't cite it as the connector limit.

### Claude apps (web / Desktop / mobile)
- Custom connectors (remote MCP) are GA across Free/Pro/Max/Team/Enterprise. No published numeric per-result cap.
- Practical reality: each connected MCP server's **tool *definitions*** also consume context every turn (reports of up to ~18K tokens/turn just for definitions) — unrelated to result size, but it eats the same budget a big transcript needs. Output-size handling is a known thing developers must debug for connectors.

**Bottom line for `ytt`:** there is **no single official "max MCP tool result size" number** you can point to for the API connector. The number you can rely on as a *design target* is the Claude Code-style **10K-token warning / 25K-token soft default**, because (a) it's the only concretely documented MCP-output number in the Claude ecosystem and (b) keeping under it keeps you safe everywhere.

---

## 3. Tool-result structure & whether resource links change the size calculus

Per the MCP spec, a tool result's `content` array can hold blocks of these types:

| Block type | Shape | Notes for `ytt` |
|---|---|---|
| `text` | `{ "type": "text", "text": "..." }` | The obvious one. **Counts fully against context** — the whole transcript lands in the window. |
| `image` | base64 `data` + `mimeType` | N/A for transcripts. |
| `audio` | base64 `data` + `mimeType` | N/A (don't ship raw audio). |
| `resource_link` | `{ "type": "resource_link", "uri": "...", "name": ..., "mimeType": ... }` | **A pointer, not the payload.** The link text is tiny; the client/model fetches the body on demand. **This is how you avoid inlining a 100 KB transcript.** |
| `resource` (embedded) | `{ "type": "resource", "resource": { "uri", "mimeType", "text" } }` | Embeds the full content inline — **same size cost as `text`**, just wrapped with a URI + MIME type. Does NOT save context. |

Key distinctions:
- **`resource_link` changes the size calculus** (small inline footprint; body fetched separately). **Embedded `resource` does not** — it's full content inline, same token cost as plain text.
- **Caveat:** "Of the feature set of the MCP specification, only **tool calls** are currently supported" by the API MCP connector. **MCP *resources* are not supported on the connector path.** So a `resource_link` pointing at an MCP `resource://` URI **won't be auto-fetchable by Claude via the connector** — the model can't follow it through MCP. To make a link actionable on the connector, point it at a plain **`https://` URL the model can fetch with a web-fetch tool**, or return the URL as text and let the user/agent retrieve it. (In Claude Code / the apps, resource support is broader, but don't assume it on the API connector.)
- `structuredContent` + `outputSchema` exist for typed/JSON results; not relevant to a free-text transcript, and the spec still wants the serialized form mirrored into a `text` block for backwards compat (so it doesn't reduce size).

**Implication:** the only structural lever that genuinely reduces what hits the context window is **returning a URL/link instead of the full text** — and on the API connector that link must be a normal fetchable URL, not an MCP `resource://`.

---

## 4. Pagination — applies to lists, NOT to tool-call results

This is the most common misconception, so stated precisely:

MCP cursor-based pagination ([spec: Pagination](https://modelcontextprotocol.io/specification/2025-06-18/server/utilities/pagination)) supports **exactly these operations**:

- `resources/list`
- `resources/templates/list`
- `prompts/list`
- `tools/list`

> "The following MCP operations support pagination: `resources/list`, `resources/templates/list`, `prompts/list`, `tools/list`."

**`tools/call` is NOT in that list.** A tool result has no `nextCursor` field and no protocol-level paging. The pagination mechanism is for *discovering* large sets of tools/resources/prompts, not for streaming a large *result* from one tool call.

**Therefore:** `ytt` cannot use MCP's built-in pagination to deliver a long transcript in pages. If you want chunked delivery, you must build it **yourself inside the tool's own arguments** — e.g., a `cursor` / `start_offset` / `chunk` input parameter, returning a slice plus a "there's more, call again with cursor=X" hint in the result text. That's application-level paging, not MCP pagination.

---

## 5. Practical patterns for returning long text from an MCP tool

Ordered roughly by how well they fit `ytt`:

1. **Conservative inline + self-paging (recommended primary).** Return the transcript inline as `text` when it's under a safe budget (target **≤ ~8–10K tokens** to stay below the Claude Code warning and well within any client). For longer videos, accept a tool argument like `cursor`/`offset` (and maybe `max_chars`/`max_tokens`), return one chunk, and include explicit follow-up instructions in the result, e.g. *"Returned segment 1/4 (00:00–28:30). To continue, call `get_transcript` again with `cursor='seg-2'`."* The model drives the loop via repeated tool calls. This is the "chunk with a follow-up get-next-chunk" pattern and is fully portable across all clients.
2. **Return a URL / `resource_link` the model fetches on demand.** Host the full transcript at a stable `https://` URL and return a short link + summary/metadata. Smallest context footprint. **On the API connector**, ensure the model has a web-fetch capability to follow it (MCP `resource://` links are not connector-supported — use a real URL). Great for "give me the gist, fetch detail if needed" flows.
3. **Server-side summarization / segmentation.** Offer a mode (or a second tool / argument) that returns a condensed transcript, per-section summaries, or only the segment matching a query (e.g., `query="part about X"`). Dramatically cuts tokens when the user doesn't need verbatim text. Pairs well with #1/#2.
4. **Time/section-range arguments.** Let the caller request `start_time`/`end_time` or a chapter, so the tool returns only the relevant slice instead of the whole 2-hour transcript.
5. **Streaming.** MCP supports Streamable HTTP transport, but **the tool *result* is still a single `CallToolResult`** — streaming the transport does not let you exceed the client's result-size handling, and the API connector consumes the final result, not a stream of partials. Use `notifications/progress` for keep-alive (see §6), not as a way to bypass size limits. Don't rely on streaming to deliver a huge result.

**Anti-patterns:** dumping a raw 100 KB transcript inline as a single `text` block and hoping the client truncates "gracefully" (it may clip mid-word, error on buffer limits, or silently cost a fortune in input tokens); assuming MCP pagination will page your result (it won't, §4).

---

## 6. Timeouts on a tool call (important — Whisper is slow)

Tool-call timeouts are **client-set, not spec-mandated**, and the common default bites long jobs:

- **Default ~60 seconds** in several MCP clients/SDKs (e.g., the TypeScript SDK historically timed out at 60 s; error `-32001` "Request timed out"). A long video + Whisper transcription can easily exceed this.
- **Mitigation — progress notifications:** a server can send `notifications/progress` during a long call. **Whether that resets the client's timeout depends on the client honoring `resetTimeoutOnProgress`** — historically **off by default** in the TS SDK (fixed in [typescript-sdk#849](https://github.com/modelcontextprotocol/typescript-sdk/pull/849); Python SDK reset correctly). So progress notifications help **only if the client opts in** — you cannot assume they do.
- **For the API MCP connector specifically:** Anthropic does not publish a configurable per-tool timeout for the connector, and the overall request is also subject to normal HTTP/streaming timeouts (the Anthropic SDKs refuse very long non-streaming requests). **Design `ytt` so the transcription work finishes fast**, or:
  - **Transcribe ahead of time / cache** so the tool call is a fast lookup, not a live Whisper run.
  - **Make the tool return quickly** (e.g., kick off transcription and return a status + `resource_link`/job id, with a second tool to poll/fetch) rather than blocking for minutes.
  - Emit `notifications/progress` as a best-effort keep-alive, knowing not every client resets on it.

**Safe assumption:** budget for a **~60 s** ceiling unless you control the client. A 2-hour Whisper transcription will not fit in that window — **pre-transcribe/cache or use an async fetch-by-id pattern.**

---

## 7. Differences across surfaces (Desktop vs mobile vs API connector)

| Surface | Result size handling | Notable specifics |
|---|---|---|
| **Claude Code (CLI)** | Soft **10K-token** warning; `MAX_MCP_OUTPUT_TOKENS` (default ~**25K**) raises it; hard **500K-character** ceiling. Display truncation bug (~700 chars shown, full body to temp file). Possible `maxBuffer` errors on huge stdio payloads. | Most explicit numbers live here. Good proxy for "safe everywhere." |
| **API MCP connector** (`mcp-client-2025-11-20`) | **No documented per-result cap.** Full result enters the context window (1M Opus / 200K Haiku), billed as input tokens, **not ZDR-covered**. Only `tools/call` supported — **no MCP resources/prompts**, so `resource://` links aren't auto-followable. | The surface `ytt` targets. Real limit = context window + your `max_tokens` + cost. |
| **Claude apps (web / Desktop / mobile)** | GA custom connectors; no published numeric cap. Large results accepted; **UI/display may truncate**. Tool *definitions* also consume context each turn. | Display truncation is cosmetic vs. model-context; mobile is just a client of the same connector backend — no separate documented number. |

There is **no published evidence of different *numeric* result caps between web, Desktop, and mobile** — they share the connector backend. The meaningful axis is **Claude Code (has explicit token/char limits) vs. the API connector (limited by context window/cost, no separate cap) vs. CMA (100K-token auto-offload)**.

---

## Recommended design for `ytt` (synthesis)

1. **Cache/pre-transcribe** so the tool call is fast (dodges the ~60 s timeout). Avoid running a multi-minute Whisper job inside a single synchronous `tools/call`.
2. **Return inline `text` only when ≤ ~8–10K tokens.** Stay under the Claude Code warning so it's safe on every client.
3. **For longer transcripts, self-paginate via a tool argument** (`cursor`/`offset`/`start_time`) and include explicit "call again with cursor=X for the next segment" guidance in the result. (MCP pagination won't do this for you.)
4. **Offer a `resource_link`/URL mode and a summary/segment mode** for callers who want the gist or a specific part without the full verbatim dump. On the API connector, make links plain `https://` URLs (not `resource://`).
5. **Never assume graceful client truncation.** Cap what you emit yourself, with metadata (`total_tokens`, `segment N of M`, `next_cursor`) so the model can decide what to fetch.
6. **Treat 25K tokens as a hard self-imposed ceiling per result** and 8–10K as the comfortable default; anything larger should be paged or linked.

---

## Sources

- MCP spec — Tools (`tools/call`, result content types, no size limit): https://modelcontextprotocol.io/specification/2025-06-18/server/tools
- MCP spec — Pagination (lists only, not tool results): https://modelcontextprotocol.io/specification/2025-06-18/server/utilities/pagination
- MCP Discussion #2211 — proposed response size limit (not adopted): https://github.com/modelcontextprotocol/modelcontextprotocol/discussions/2211
- Anthropic — MCP connector docs (API, `mcp-client-2025-11-20`; only tool calls supported; not ZDR): https://platform.claude.com/docs/en/agents-and-tools/mcp-connector
- Anthropic — Get started with custom connectors using remote MCP: https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
- Connect to remote MCP servers (modelcontextprotocol.io): https://modelcontextprotocol.io/docs/develop/connect-remote-servers
- Claude Code MCP docs (10K-token warning, `MAX_MCP_OUTPUT_TOKENS`, 500K-char ceiling): https://code.claude.com/docs/en/mcp
- claude-code#2638 — truncated MCP tool responses (~700 char display, maxBuffer): https://github.com/anthropics/claude-code/issues/2638
- Morph — "MCP Output Too Large: Why Tool Results Exceed Token Limits" (25K default context): https://www.morphllm.com/mcp-output-too-large
- TypeScript SDK #245 / PR #849 — 60s timeout, `resetTimeoutOnProgress`: https://github.com/modelcontextprotocol/typescript-sdk/issues/245 and https://github.com/modelcontextprotocol/typescript-sdk/pull/849
- MCPcat — Fixing MCP error -32001 request timeout: https://mcpcat.io/guides/fixing-mcp-error-32001-request-timeout/
- Anthropic Claude API skill (cached) — CMA auto-offloads MCP tool outputs > 100K tokens to a sandbox file (CMA-specific, not the connector).
