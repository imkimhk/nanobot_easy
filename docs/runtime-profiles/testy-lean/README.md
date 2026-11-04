# Testy runtime profile snapshot

This directory records the active Testy instructions, on-demand skill documents,
and non-secret response-related settings as observed on 2026-11-02.
The running product code is commit `5cbb18b7ff2c9b03c96492b87d40d9db44b41837`
in the separate `testy-runtime-5cbb18b7` checkout. That code is not included
in this repository by this snapshot.

`settings.fragment.json` is a partial configuration reference, not a standalone
configuration file. It excludes credentials, channels, workspaces and unrelated
agent settings. `contextSelection.enabled` is false, so the ordinary agent loop
is the active response path even though the time-allocation contract flag is true.
The two skill bodies are loaded when needed; they are not preloaded.

This snapshot does not alter a running agent or apply configuration.

Status note (checked 2026-11-04): this directory remains a dated 2026-11-02
record. Testy no longer runs `5cbb18b7`; as of 2026-11-04 the local instances,
Testy included, run the pinned integration runtime `3933fed9` (see
[`deploy/README.md`](../../../deploy/README.md) for the pinned-runtime method).
The instructions and settings captured here have not been re-verified against
that runtime.
