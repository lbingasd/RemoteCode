---
name: remotecode-qq
description: Send an authorized task screenshot to the QQ user bound to this RemoteCode machine. Use when the user asks to receive screenshots in QQ or supplies a screenshot for remote review.
---

Use the local RemoteCode service to send PNG/JPEG screenshots, at most 10 MiB:

```sh
"@PYTHON@" "@SCRIPT@" send-image --root "@ROOT@" --path "/absolute/path/to/screenshot.png"
```

If the current ACP session ID is known, append `--session "SESSION_ID"`; this lets the owner retrieve its most recent screenshot with an approval number. Do not invent a session ID. Otherwise use the returned screenshot number in QQ.

Only send images covered by the user's request to the configured recipient. Check the image for unrelated windows or secrets before sending. An existing screenshot is sufficient; this skill does not provide a desktop capture tool. Use an available authorized browser/screenshot tool if a fresh screenshot is requested.

The local service performs QQ authentication, upload, and binding checks. Never read or display AppSecret, local tokens or binding files. If the service is unavailable, report the failure instead of uploading elsewhere.

ACP approvals are handled by the QQ approvals ACP proxy（Codex/Claude）, independently of this skill. Do not translate natural language into arbitrary shell execution, generate approval responses, or approve on the user's behalf. QQ decisions only select options from an existing pending request.
