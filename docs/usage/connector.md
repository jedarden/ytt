# Adding ytt as a Claude Connector

This guide covers adding ytt as a custom MCP connector in Claude Desktop and
verifying it works on mobile.

## Prerequisites

- ytt server running and accessible over HTTPS.
- `YTT_PUBLIC_URL` set to your public URL (e.g. `https://mcp.ardenone.com/ytt`).
- `YTT_ALLOWED_SUBJECTS` is initially empty — the server will deny all tool calls
  until you complete steps 2–4 below.

## Step 1: Add the connector in Claude Desktop

1. Open Claude Desktop.
2. Go to **Settings → Connectors → Add MCP Server** (or similar menu path).
3. Enter the connector URL: `https://your-domain.example.com/ytt`
4. Click **Connect** and complete the OAuth flow (browser opens for auth).
5. The connector should appear as "ytt" in your connector list.

At this point, tool calls will return a `403 Forbidden` because
`YTT_ALLOWED_SUBJECTS` is empty.  That's expected.

## Step 2: Discover your allowed-subject value

The allowlist (`YTT_ALLOWED_SUBJECTS`) is checked against the token's
**verified email claim**, not an opaque `sub` — see `ytt/authz.py`'s
`check_subject_auth`. This applied under the earlier Google-federated setup
and is unchanged by the ADR-003 move to Authentik (`docs/plan/plan.md`):
Authentik's default OpenID scope mapping populates `email`/`email_verified`
the same way. (`ytt selftest --show-sub` is a historical name — it reports
the email value, not a `sub` claim.)

In the ytt pod (or wherever the server is running):

```bash
ytt selftest --show-sub
```

This reads `/tmp/ytt_last_sub` — a file written by the server on the first
successful OAuth token decode.  The file is mode 0600 and is never logged.

Example output:
```
sub: me@jedcabanero.com
```

## Step 3: Allow your subject

Update the `YTT_ALLOWED_SUBJECTS` environment variable with the email value
(exact address, or an `@domain` pattern to allow an entire domain — see
`ytt/authz.py`'s `subject_allowed`):

```bash
# Direct (env var):
export YTT_ALLOWED_SUBJECTS="me@jedcabanero.com"

# Kubernetes (via OpenBao/ESO on ardenone-cluster):
bao kv put secret/ardenone-cluster/ytt/allowed-subjects \
  value="me@jedcabanero.com"
# Wait for ExternalSecret to resync, then restart the pod if needed.
```

For multiple users, comma-separate the subjects:
```
YTT_ALLOWED_SUBJECTS="sub1,sub2,sub3"
```

## Step 4: Verify

In Claude Desktop (with the connector added), ask:
> "Get the transcript of https://www.youtube.com/watch?v=jNQXAC9IVRw"

Expected: the transcript of "Me at the zoo" (YouTube's first video, 18 seconds).
If you get a 403, recheck the `sub` value (copy-paste carefully).

## Step 5: Mobile

Mobile reuses the web/Desktop OAuth token — no separate setup needed.
Open Claude on iOS/Android and send the same request.

**Note:** You cannot ADD a connector from mobile (no settings UI).  Add it
on Desktop first; mobile picks it up automatically.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `403 Forbidden` | `sub` not in allowlist | Run `ytt selftest --show-sub`, copy the sub exactly, update `YTT_ALLOWED_SUBJECTS` |
| `401 Unauthorized` | Token expired or wrong audience | Check `YTT_PUBLIC_URL` matches the connector URL exactly (no trailing slash) |
| `pending` response, never resolves | Whisper is down | Check `YTT_WHISPER_URL` is reachable; call `GET /admin/egress` for diagnostics |
| No connector in mobile | Not added on Desktop | Add on Desktop first |
| "Can't connect to server" | HTTPS issue | Verify cert is valid; check `/ytt/health` |

## Security notes

- The `YTT_ALLOWED_SUBJECTS` list is the primary per-user authorization gate.
  Any OAuth user who learns the connector URL can complete the OAuth flow, but
  they will receive `403 Forbidden` on all tool calls unless their `sub` is in
  the allowlist.
- The `sub` value is not a password — it is an identifier.  It is safe to
  share with the server operator.  It is NOT safe to log or expose the full
  `YTT_ALLOWED_SUBJECTS` list (treated as sensitive data by the redaction filter).
