# Codex GPT-6 compatibility record

## Scope and status

This profile applies only to `gpt-6-astra` and `gpt-6.1-sol` on `/v1/responses`.
It uses native Responses on each configured deployment at
`/responses?api-version=2025-03-01-preview`. Missing target models return 404;
empty deployment lists are configuration errors. Other endpoints retain their routing.

Use **one verified deployment per model** for Codex. Proxy round robin is retained,
but opaque reasoning portability across deployments or models has not been verified.
Live command/edit/resume/compaction/cancellation, correlated opaque reasoning replay,
and interactive `--no-daemon` checks pass for both models with Codex 0.164.0.

## Request contract

Accepted fields: `model`, `input`, `instructions`, `tools`, `tool_choice`,
`parallel_tool_calls`, `reasoning`, `include`, `text`, `prompt_cache_key`,
`client_metadata`, `stream`, `max_output_tokens`, `store`.

- `input` is text or an array of message, function-call, function-result, and
  reasoning items. IDs, phases, arguments, results, schemas, and opaque reasoning
  are preserved, without orphan removal or reordering.
- Ordinary function tools and namespace containers with ordinary function children
  are supported; strict JSON schemas stay intact. The proxy flattens namespace
  identities into deterministic gateway aliases and restores namespace/name in
  client output/history. Call/item IDs, arguments/results and ordering stay intact.
- `store` defaults to false; `stream` defaults to false. Both must be booleans.
- Inactive `background: false` and `previous_response_id: null` are omitted.
  Storage, background work, and server-managed continuation are rejected.
- Replay full same-model history, including encrypted reasoning, from the client.
- Lite headers/items/`additional_tools`, custom/hosted tools, nested namespaces, Lite
  reasoning-context overrides, and unknown semantic fields return field-specific 400s.
- Effort and summary are forwarded without coercion. Low/high are the exercised
  efforts; upstream validates other values. `include: ["reasoning.encrypted_content"]`
  and `max_output_tokens` retain their native meanings.
- Accepted cache/metadata fields do not establish actual caching behavior.
- Credentials, deployment selection, tenant, and resource group remain operator-owned.

## HTTP transport and errors

The selected deployment base URL receives `/responses?api-version=2025-03-01-preview`.
Native inference uses a separate pooled session with **zero automatic retries**, no
redirect following, a 15-second connect timeout, and a 300-second timeout per
blocking read. Existing OAuth caching/control-plane HTTP behavior is independent.
Clients own any retry policy; the proxy does not switch deployments or downgrade
to Chat Completions after failure.

Upstream is opened before downstream streaming headers. HTTP validation/authentication/
permission/rate-limit errors retain upstream status, safe error type/code/parameter,
and redacted diagnostics. `Retry-After` is retained for 429/503. Unexpected redirects
and transport errors map to 502; request-open timeouts map to 504. Invalid native
JSON maps to 502. Every non-streaming response is closed, including error paths.
Completed, incomplete, and failed native JSON retains its original status, IDs,
output, reasoning, usage, and error fields.

## Streaming lifecycle and cancellation

Native SSE forwards validated frames incrementally, preserving IDs, sequence numbers,
tool arguments/items, opaque reasoning and usage. Completed, incomplete and failed
terminal events are forwarded as distinct outcomes and stop upstream reads. No
synthetic completed call, successful terminal event or `[DONE]` is appended.
Malformed events, premature EOF, and read failures emit an `error` event when writable.
The translated Responses path also requires a legitimate Chat finish signal or
`[DONE]`; exceptions/content-only EOF no longer fabricate successful completion.

Upstream ownership is request-scoped and closes idempotently on terminal events,
errors, iterator closure or Flask response closure, including unconsumed responses.
The synchronous serving stack detects disconnects during writes; while blocked
waiting for a silent upstream, the 300-second **per-read** timeout bounds that read.
Remote model computation cancellation is not guaranteed. Loopback HTTP tests under
threaded Werkzeug WSGI use a 0.2-second read timeout and verify detected disconnect
closure within one second, plus an unaffected request for the other model. These
are transport fixtures, not production Waitress or live SAP cancellation evidence.

Optional lifecycle logging records the actual native stage-2 request and terminal
response. Partial failures are logged as failed with available usage; logging errors
cannot break delivery. Native forwarding does not retain a second chunk transcript.

## Catalog generation and configuration

Use the installed Codex 0.164.0 binary's bundled catalog; the generator retains its
model instructions and selects only the two exact slugs. Supply an explicit token
context limit for **each SAP deployment**:

```sh
python examples/codex/generate_catalog.py \
  --astra-context-limit 272000 --astra-max-context-limit 872000 \
  --sol-context-limit 272000 --sol-max-context-limit 872000 \
  --output examples/codex/models.json
```

The output path must not already exist. Generation reports binary version, canonical
source-catalog SHA-256, operator limits and capability overrides. Compaction is set
to 90% of each supplied base limit; extended limits are enabled only when explicitly
supplied. First-party speed/tier claims are cleared. Bundled model instructions are preserved. The required web-search
format enum remains in the catalog; actual hosted web search is disabled in TOML.

Explicitly apply `examples/codex/config.toml.example`, replacing `model_catalog_json`
with an **absolute** generated path. It selects HTTP/SSE Responses, the separate
`SAP_AI_PROXY_API_KEY` proxy token, no hosted web/image tools or WebSockets, and local
compaction. Catalog loading happens at startup: restart Codex with `--no-daemon`
after changing the catalog or exported token. Proxy startup does not edit global
Codex configuration. Optional tools are disabled for this profile; actual emitted
requests, namespace execution and coding-loop acceptance have passed.

On 2026-10-24, Codex 0.164.0 loaded generated JSON in an isolated temporary home and
loaded the example with `codex exec --strict-config`; absence of the proxy token
produced the expected named-variable error. The test-only 32000/64000 context values
are synthetic fixture inputs, **not SAP deployment limits**. Source catalog canonical
SHA-256: `087e70016dd359fc16e70ba9fd009d57fa9334cfa46b8de54fa007c03e9edd98`.

### Operator-selected context settings — 2026-10-24

The operator selected Codex 0.164.0's bundled values for both models:
**272,000 base tokens**, **872,000 extended maximum**, and **244,800 base-window
compaction threshold**. These explicit settings are sourced from the bundled catalog;
SAP capacity at these sizes remains unverified. The extended maximum is an override
ceiling; the normal example runs at the 272k base window. To explicitly select the
extended window, launch with both window and compaction overrides:

```sh
codex --no-daemon --model gpt-6.1-sol \
  -c model_context_window=872000 -c model_auto_compact_token_limit=784800
```

## Evidence layers

### Actual Codex offline runner — 2026-10-24

Prerequisites: installed **Codex 0.164.0**, Python dependencies used by the proxy,
and permission to bind loopback HTTP ports. No SAP credentials are needed.

```sh
python tests/codex_integration.py --offline
# Select another installed binary explicitly:
python tests/codex_integration.py --offline --codex /absolute/path/to/codex
```

This explicit runner is separate from unittest discovery. Eight checks pass (56.256s),
covering both model selections: actual native tool envelope, workspace-write command/file
edit and matching result replay, saved-session follow-up/resume, forced local compaction,
failed/incomplete/truncated partial tool streams, and SIGINT cancellation during a
stalled upstream. Cancellation releases owned responses within the fixture's two-second
read timeout plus one-second detection allowance; independent subsequent requests pass.
Additional checks verify that shell tools cannot inherit the client proxy token or
exported SAP service-key variable, interactive startup completes, and a real
namespaced worktree-listing function executes and replays its matching result.
Compaction uses synthetic usage and a test-only 10k threshold. The selected 272k/872k
catalog is loaded normally; this is not a SAP context-capacity stress test.

Temporary Codex home/workspace, synthetic token and fake native backend behind the
real proxy isolate the test. Commands remain under Codex's workspace-write sandbox
with approval policy `never` (sandbox failures are returned to the client). A missing
binary returns an explicit blocked outcome. Binary version and source-catalog digest
are recorded above; rerun on upgrade. Actual request inspection found that disabling
feature flags alone retained a collaboration namespace: `[agents] enabled=false` is
required by this binary for the compatible baseline.
The shell-environment policy keeps authentication in the Codex process while excluding
proxy/SAP secrets from model-invoked commands. Final-answer grounding is checked on
completed assistant messages, not command-output transcripts; compaction requires
the actual matching tool result in the received summarization input.

### Opt-in live runner

Start the updated proxy independently using the operator's existing configuration,
then export `SAP_AI_PROXY_API_KEY` in the test terminal. Explicit live selection is
required; it never runs by default:

```sh
python tests/codex_integration.py --live --proxy-url http://127.0.0.1:3001/v1
```

Live mode uses temporary Codex home/workspaces and synthetic files. It prints only
capability outcomes and catalog/version metadata, keeping client output and credentials
out of the report. Command/edit grounding and history-only follow-up are checked on
completed assistant messages and successful tool execution. Where protocol evidence
is unavailable (reasoning usage/replay, active-inference cancellation and remote
ownership cleanup), the report remains explicitly blocked. Use instrumented mode
for those checks. Interactive `--no-daemon` uses a real PTY and saved completed
assistant output, never echoed prompt text. SAP capacity stress testing is separate.
Live runs were authorized and performed on 2026-10-24. The regular proxy ran separately
at port 3001 using existing operator configuration. A temporary instrumented proxy
using the same configuration at port 3002 recorded request-correlated capability facts
and opaque hashes without retaining native transcripts:

```sh
python tests/codex_integration.py --live --proxy-url http://127.0.0.1:3002/v1 \
  --instrument-proxy-config config.json
```

The instrumented port must be free. This option starts and closes only its own
temporary serving stack; the independent operator proxy remains running.

### Live full-chain results — 2026-10-24

| Capability | Astra | Sol |
| --- | --- | --- |
| Native streaming, command execution, independently inspected file edit | Pass | Pass |
| Matching tool-result continuation and grounded final answer | Pass | Pass |
| Same-model saved-session resume and history-only follow-up | Pass | Pass |
| Controlled local compaction with usable next turn | Pass | Pass |
| SIGINT after the cancellation request opens native upstream | Pass | Pass |
| That exact request closes unsuccessfully within read/detection bounds | Pass | Pass |
| Subsequent independent request / proxy lifetime | Pass | Pass |
| High effort emitted; positive reasoning usage | Pass; 775 reasoning tokens | Pass; 865 reasoning tokens |
| Successful correlated same-session opaque reasoning replay | Pass | Pass |
| Interactive `--no-daemon` under a real PTY | Pass | Pass |

Counts are cumulative across each model's synthetic live acceptance run. The initial
reasoning evidence compared terminal-copy encrypted values. SAP returns different
ciphertext in completed streamed items and terminal copies; Codex correctly persists
and replays the streamed item. Corrected evidence hashes delivered completed reasoning
items and requires a successful same-session replay after positive high-effort usage.
Neither opaque value is changed by the proxy.

Codex 0.164.0's interactive TUI injects `mcp__codex_worktrees`; the legacy worktrees
flag is removed/ignored. The approved namespace-function adapter resolves this:
deterministic unique aliases remain at most 64 gateway-safe characters, while JSON/SSE
added/done/terminal function identities and response tool metadata restore client
namespace/name. Historical namespaced calls map across resume without proxy storage.
Duplicate/malformed/nested/custom identities and collisions return 400. Unknown nested
application data is never traversed as protocol identity locations. Codex retains
ownership of tool execution, sandbox and approvals.

Environment: Python 3.13.7, Flask 3.1.2, requests 2.32.5, urllib3 2.5.0,
Werkzeug 3.1.4, sap-ai-sdk-gen 7.4.1. Waitress was not installed: regular serving used
Flask/Werkzeug and instrumentation used threaded Werkzeug WSGI. These results do not
establish production Waitress cancellation or 272k/872k SAP capacity.

### Direct backend probes — 2026-10-24

These are prior exploration results from the approved design, not full-chain acceptance.
Codex binary observed: 0.164.0. Separate read-only source observation: revision
`970111f6cb`. SAP calls used existing service-key OAuth, resource group `default`,
and API version `2025-03-01-preview`.

| Probe | Astra | Sol |
| --- | --- | --- |
| Native JSON greeting | Pass | Pass |
| Native SSE function call and matching newly generated result | Pass | Pass |
| High-effort arithmetic with opaque reasoning | Pass; 175 reasoning tokens | Pass; 179 reasoning tokens |
| Complete output replay on same-model follow-up | Pass | Pass |
| Low effort, output limit 16 | Incomplete: max_output_tokens | Incomplete: max_output_tokens |
| Top-level custom grammar tool | Rejected: HTTP 400 | Rejected: HTTP 400 |
| Top-level namespace tool | Rejected: HTTP 400 | Rejected: HTTP 400 |
| Synthetic Lite additional_tools function round trip | Limited probe pass | Limited probe pass |
| Standard Codex envelope fields together | Accepted | Accepted |
| Effort none / ultra | Rejected: HTTP 400 | Rejected: HTTP 400 |

Gateway errors advertised low, medium, high, xhigh, max. Only low/high were exercised.
Limited Lite behavior is not an advertised capability of this profile.

### Offline proxy verification

Baseline on 2026-10-24: `python -m unittest discover -p 'test_*.py'` — 138 passing
characterization tests. Added native tests cover routing, request fidelity/rejection,
client authentication, selected-subaccount token refresh, and header isolation.
Latest full offline suite on 2026-10-24: **199 tests pass** (7.636s), including
loopback HTTP/WSGI ownership, logging failure, Unicode/SSE diagnostic handling and
installed-binary catalog/strict-configuration loading and live-runner boundaries.
Actual Codex coding-loop checks passed separately against the synthetic local backend.
The complete explicit actual-Codex `--offline` runner passes all eight checks,
including interactive namespace execution and completed interactive startup.

### Full Codex acceptance

| Check | Astra | Sol |
| --- | --- | --- |
| Offline native Codex emitted tools/history envelope | Pass: Codex 0.164.0 | Pass: Codex 0.164.0 |
| Offline actual command and file edit | Pass | Pass |
| Offline tool-result continuation | Pass | Pass |
| Offline follow-up and saved-session resume | Pass | Pass |
| Offline local compaction and cancellation | Pass | Pass |
| Live command/edit/result continuation/resume/compaction | Pass | Pass |
| Live active-turn cancellation and request-owned cleanup | Pass | Pass |
| Live high-effort opaque history replay | Pass | Pass |
| Interactive startup | Pass | Pass |
| Independent proxy lifetime | Pass | Pass |
| Operator context settings | Selected: 272k base / 872k extended | Selected: 272k base / 872k extended |
| SAP capacity at selected context settings | Unverified | Unverified |

No credential values, private endpoints, opaque reasoning contents, or private
transcripts are included in this record.
