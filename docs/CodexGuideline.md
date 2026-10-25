# Codex CLI with SAP AI Core

Use the proxy's `/v1/responses` endpoint to connect Codex CLI to configured SAP AI Core deployments.
The native profile targets Codex CLI 0.164.0 and the exact models `gpt-6.1-sol` and
`gpt-6-astra`. Native backend probes, offline protocol/configuration checks, live coding
workflows, opaque reasoning replay and interactive startup pass. See the
[compatibility record](./CodexCompatibility.md) for tested versions and limits.

## 1. Configure the proxy

Create or update `config.json` in the project root. Use the service key and resource group
associated with your deployments:

```json
{
    "subAccounts": {
        "subAccount1": {
            "resource_group": "default",
            "service_key_json": "subAccount1_service_key.json",
            "deployment_models": {
                "gpt-6.1-sol": [
                    "https://<ai-core-host>/v2/inference/deployments/<sol-deployment-id>"
                ],
                "gpt-6-astra": [
                    "https://<ai-core-host>/v2/inference/deployments/<astra-deployment-id>"
                ]
            }
        }
    },
    "secret_authentication_tokens": ["<your-chosen-proxy-token>"],
    "port": 3001,
    "host": "127.0.0.1"
}
```

Generate a proxy token with:

```sh
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Replace `<your-chosen-proxy-token>` with the generated value. The two authentication steps are:

```text
Codex -> proxy: chosen token from secret_authentication_tokens
Proxy -> SAP AI Core: OAuth token fetched using the SAP service key
```

`config.json` and service-key JSON files are ignored by Git.

### Credentials from AICORE_SERVICE_KEY

The proxy's GPT HTTP path reads a service-key file; it does not directly load `AICORE_SERVICE_KEY`.
If that exported environment variable already contains the standard service-key JSON, run this
from the project root to create the file referenced above:

```sh
python - <<'PY'
import json
import os

key = json.loads(os.environ["AICORE_SERVICE_KEY"])
fd = os.open(
    "subAccount1_service_key.json",
    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
    0o600,
)
with os.fdopen(fd, "w") as file:
    json.dump(key, file, indent=2)
PY
```

This creates a new file without overwriting an existing one. If your key file already exists,
set `service_key_json` to that file's path. The proxy fetches, caches, and refreshes SAP OAuth
tokens automatically; Codex does not need the SAP access token.

## 2. Start the proxy

From the project root, in one terminal:

```sh
python proxy_server.py --config config.json
```

Check the server and model registration:

```sh
curl --fail http://127.0.0.1:3001/health
curl --fail http://127.0.0.1:3001/v1/models
```

Both configured model names should appear. Native Responses requests for either
unregistered target model return 404. Use one verified deployment per model; opaque
reasoning replay across multiple deployments has not been established.
Restart the proxy after changing its configuration or authentication tokens.

## 3. Configure Codex

Install or update the CLI:

```sh
npm install -g @openai/codex@0.164.0
```

Generate the compatible two-model catalog from the installed binary, from this
repository's root. The operator-selected values below come from Codex's bundled
catalog; SAP capacity at these sizes remains unverified:

```sh
python examples/codex/generate_catalog.py \
  --astra-context-limit 272000 --astra-max-context-limit 872000 \
  --sol-context-limit 272000 --sol-max-context-limit 872000 \
  --output examples/codex/models.json
```

The output must not already exist. Generation keeps model-specific instructions,
enables direct function tools, disables Lite/freeform patching, and records version
and digest provenance. Both models have 272k base / 872k extended settings; local
compaction starts at 244,800 base-window tokens. The extended maximum is a ceiling
for an explicit override; the normal example uses the base window. To use 872k:

```sh
codex --no-daemon --model gpt-6.1-sol \
  -c model_context_window=872000 -c model_auto_compact_token_limit=784800
```

Explicitly merge [config.toml.example](../examples/codex/config.toml.example) into
`~/.codex/config.toml`, keeping top-level settings before tables and avoiding duplicate
keys. Replace `model_catalog_json` with the generated file's absolute path, obtainable with:

```sh
python -c 'from pathlib import Path; print(Path("examples/codex/models.json").resolve())'
```

The example selects HTTP/SSE Responses, disables hosted web/image tools, uses local
compaction and exposes low/high reasoning. Catalog changes take effect at Codex startup.
Proxy startup and catalog generation do not modify global Codex configuration.
The example also filters command environments so proxy/SAP credentials stay in the
client process rather than being inherited by model-invoked shell tools.

`model_provider` must match the provider table name. `env_key` names the environment variable
Codex reads; it is not a token issued by SAP. Its value must match a token in the proxy config.

## 4. Launch interactive Codex

In a second terminal, from the proxy project root, load the token without copying it:

```sh
export SAP_AI_PROXY_API_KEY="$(python -c 'import json; print(json.load(open("config.json"))["secret_authentication_tokens"][0])')"
```

Then move to the codebase you want Codex to work on and launch:

```sh
codex --no-daemon --model gpt-6.1-sol
```

Or select Astra:

```sh
codex --no-daemon --model gpt-6-astra
```

Interactive startup was verified for both models. Namespace function containers,
including the TUI's `mcp__codex_worktrees`, are mapped to gateway-compatible function
names and restored for Codex. Custom/freeform tools, nested namespaces and Lite remain
unsupported. Codex owns tool execution and sandboxing; the proxy never executes tools.

### Why --no-daemon matters

Codex CLI 0.164.0 can reuse a persistent background app server. If that server started before
`SAP_AI_PROXY_API_KEY` was exported, the interactive session can report:

```text
Missing environment variable: SAP_AI_PROXY_API_KEY
```

This was reproduced even when the new CLI process received the token. Restarting only the
interactive CLI did not resolve it because the background server remained running.
`--no-daemon` bypasses that server and lets the session use the current terminal's environment.

A successful `codex exec` request does not prove the existing interactive daemon has the token.
During setup, verify interactive mode too.

## 5. Reasoning effort

Codex accepts a top-level setting:

```toml
model_reasoning_effort = "high"
```

Or a launch override:

```sh
codex --no-daemon --model gpt-6.1-sol -c 'model_reasoning_effort="high"'
```

The native path forwards `reasoning.effort`, `max_output_tokens`, function-call history
and encrypted reasoning unchanged. Low/high were exercised directly against both
backends; additional advertised efforts remain untested. Select either model through
the same running proxy. Keep resumed history on the same model and verified deployment.

## Rollback

Restore the previous proxy revision and prior Codex provider/catalog settings, then
restart both processes. Native-only reasoning/history may require a fresh session on
the legacy translated path; service-key and proxy configuration need no migration.

## Troubleshooting

### Native stream lifecycle

The native GPT-6 Responses path preserves upstream completed/incomplete/failed
outcomes and tool/reasoning history. A read error, malformed event or premature end
is unsuccessful; the translated Responses path now also requires an upstream finish
signal before reporting completion. Owned upstream connections close when the server
detects downstream closure. A synchronous read waiting on silent upstream data can
delay detection until its 300-second per-read timeout; cancelling Codex does not
guarantee immediate cancellation of remote computation. See the
[compatibility record](./CodexCompatibility.md) for transport tests and pending
full Codex acceptance.

### Missing environment variable

Exit the existing Codex session, export the token in the same terminal used to launch Codex,
and include `--no-daemon`. Check availability without printing the token:

```sh
python -c 'import os; print("Token available:", bool(os.getenv("SAP_AI_PROXY_API_KEY")))'
```

### HTTP 401 from the proxy

Ensure the variable's value matches `secret_authentication_tokens` and restart the proxy
after modifying that list.

### Check the Responses endpoint directly

After exporting the proxy token:

```sh
curl --fail-with-body http://127.0.0.1:3001/v1/responses \
  -H "Authorization: Bearer $SAP_AI_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-6.1-sol","input":"Reply with hello.","stream":false}'
```

### Inspect backend failures

Use the proxy terminal's error output. To capture request transformations, restart with:

```sh
python proxy_server.py --config config.json --debug --log-requests
```

Request lifecycle files are written under `logs/requests/YYYY-MM-DD/`.

## References

- [Codex CLI](https://github.com/openai/codex)
- [Codex configuration](https://github.com/openai/codex/blob/main/docs/config.md)
- [SAP AI Core service-key setup](https://sap.github.io/ai-sdk/docs/java/connecting-to-ai-core)
