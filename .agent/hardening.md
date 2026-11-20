# Hardened operating profile

Set `tools.hardenedMode: true` and `tools.restrictToWorkspace: true` for a local
assistant that should use normal workspace file tools without native code execution.
This removes shell/CLI, subagent delegation, self-modification, workspace Python
adapters, entry-point tools and MCP servers. WebUI workspace/full-access changes
are disabled. This is an application capability restriction, not an OS sandbox.
Enabling those capabilities again requires an administrator to edit the configuration.

Agent file tools, file discovery/grep and WebUI text previews deny credential paths
(including `.secrets`, `.env`, private key files and symlink aliases). Keep credentials
outside the workspace even with these checks. Configuration saves use owner-only
permissions. CLI logs redact known configuration/environment credentials and common
token formats after exception formatting; exception local variables are disabled.
Previously exposed service tokens still need revocation at the issuing service.

WebUI validates Host and Origin before HTTP/WS handling. Reverse proxies must have
explicit `channels.websocket.allowedHosts` and, if needed, `allowedOrigins`; token
issuance behind a nonlocal proxy requires a configured token/tokenIssueSecret.
Anonymous token issuance is restricted to direct local browser access.

Remote images/videos require an explicit load action. Brand/link favicons never
use remote fallback services. Local signed attachments keep working.

Chat deletion defaults to also removing that session's raw conversation-memory
events, search indexes, entities and event-derived tasks. The UI can retain these
explicitly. Curated Markdown summaries, separate backups, provider/channel records
and forensic remnants outside the live SQLite tables are not covered by chat deletion.
Existing conversations are not purged when deploying this profile.
