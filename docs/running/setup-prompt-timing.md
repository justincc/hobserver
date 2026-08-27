# Setting up prompt timing

The Turns tab consumes the ATOF JSONL stream that Hermes Agent produces.

## Hermes Agent (producer) configuration

### Which setup you need

hermes changed how the ATOF exporter is configured. Pick by your version:

| Your hermes | Exporter is configured by | Follow |
|---|---|---|
| Release **v0.20.5 (v2026.8.19)** or newer, or a `main` checkout from **2026-08-10** onward | A NeMo Relay `plugins.toml` selected by `HERMES_NEMO_RELAY_PLUGINS_TOML` | [Current hermes](#hermes-agent-v0205-v2026819-onwards) |
| A release older than **v0.20.5 (v2026.8.19)** — the bundled plugin was removed on 2026-08-10, before that release | Bundled `observability/nemo_relay` plugin + `HERMES_NEMO_RELAY_ATOF_*` env vars | [Released hermes](#hermes-agent-before-v0205-v2026819) |
| A `main` checkout between **2026-08-03 and 2026-08-10** | Either — see [The changeover](#the-changeover) | Either section |

Symptom of getting this wrong: the log simply stops growing. Both mechanisms
**fail open** and report nothing when they are not active — a missing or
stale file is the only signal. `hermes doctor` on a current checkout flags
leftover `HERMES_NEMO_RELAY_ATOF_*` vars when no `plugins.toml` is selected.

### Hermes Agent v0.20.5 (v2026.8.19) onwards

Hermes Agent core now owns the Relay session/turn/LLM/tool lifecycles, but runs
**no exporters** unless you hand it a standard Relay `plugins.toml`. The old
`HERMES_NEMO_RELAY_ATOF_*` (and `_ATIF_*`) env vars are ignored.

1. **Install the NeMo Relay SDK** into the environment that runs
   hermes-agent (the agent venv the WebUI points at, for WebUI use):

   ```bash
   uv sync --extra nemo-relay   # source checkout
   # or: pip install nemo-relay
   ```

2. **Write a `plugins.toml`**, e.g. `~/.hermes/nemo-relay/plugins.toml`. This
   is a Relay `PluginConfig`: a top-level `version`, then a list of
   `[[components]]`. The ATOF file exporter is the `observability` component;
   its own config `version` must be **3**, and the file sink needs an explicit
   `type = "file"`:

   ```toml
   version = 1

   [[components]]
   kind = "observability"
   enabled = true

   [components.config]
   version = 3

   [components.config.atof]
   enabled = true

   [[components.config.atof.sinks]]
   type = "file"
   output_directory = "/home/<you>/.hermes/nemo-relay/atof"
   filename = "hermes-atof.jsonl"
   mode = "append"
   ```

   Keep `append` (hobserver's tailer reads incrementally and treats a
   shrinking file as a rotation). The three sink fields are the direct
   equivalents of the old `HERMES_NEMO_RELAY_ATOF_OUTPUT_DIRECTORY`,
   `_FILENAME`, and `_MODE`.

3. **Point hermes at it** in `~/.hermes/.env`:

   ```bash
   HERMES_NEMO_RELAY_PLUGINS_TOML=/home/<you>/.hermes/nemo-relay/plugins.toml
   ```

   `~/.hermes/.env` is loaded by hermes itself at startup
   (`hermes_cli/env_loader.py`), so it reaches the agent under any launcher.
   The variable is process-wide for every profile. Restart hermes after
   editing — the file is read once at startup.

4. **Verify** — see [Verify](#verify).

### Hermes Agent before v0.20.5 (v2026.8.19)

On any released hermes, ATOF export is a bundled plugin you enable and
configure through env vars.

1. **Install the NeMo Relay SDK** into the agent's environment:

   ```bash
   pip install "nemo-relay==0.3"
   # or, for a hermes-agent source checkout:
   uv sync --extra nemo-relay
   ```

2. **Enable the bundled plugin** (against the same `HERMES_HOME` the agent
   runs with — usually the default `~/.hermes`):

   ```bash
   hermes plugins enable observability/nemo_relay
   ```

3. **Configure the exporter in `~/.hermes/.env`:**

   ```bash
   HERMES_NEMO_RELAY_ATOF_ENABLED=1
   HERMES_NEMO_RELAY_ATOF_OUTPUT_DIRECTORY=/home/<you>/.hermes/nemo-relay/atof
   HERMES_NEMO_RELAY_ATOF_MODE=append
   ```

   The directory is created automatically; the default filename is
   `hermes-atof.jsonl` (`HERMES_NEMO_RELAY_ATOF_FILENAME` overrides it). Keep
   `append` mode. Restart hermes after editing.

4. **Verify** — see [Verify](#verify).

### The changeover

The migration happened on `main` in stages, so a source checkout can sit on
either side:

- **2026-08-03** — the `HERMES_NEMO_RELAY_PLUGINS_TOML` path lands. This is
  the earliest a `plugins.toml` can be used; before it, only the bundled
  plugin and its env vars exist.
- **2026-08-03 → 2026-08-10** — overlap: the bundled plugin is still present
  *and* the `plugins.toml` path works, so you can migrate early with either
  method active.
- **2026-08-10** — the bundled `observability/nemo_relay` plugin is removed.
  From here the `plugins.toml` is the only way; `hermes plugins enable
  observability/nemo_relay` errors and the `HERMES_NEMO_RELAY_ATOF_*` vars go
  dead. A config migration also strips the plugin from `plugins.enabled` and
  leaves a warning.

So if you are pinned to a release older than v0.20.5 (v2026.8.19) you cannot
adopt the `plugins.toml` early — the mechanism isn't there. On `main`, prefer the `plugins.toml` method any
time from 2026-08-03: it is where hermes is going and survives the removal.

### Verify

Same check either way — a one-shot CLI run should grow the log:

```bash
hermes chat -q "Reply exactly ok"
wc -l ~/.hermes/nemo-relay/atof/hermes-atof.jsonl
```

A missing or unchanged file means the exporter is not active: the SDK is not
installed in that venv, the config is not reaching the process, or (current
hermes) the `plugins.toml` shape was rejected or ignored. Nothing fails
loudly — check `hermes doctor` and validate the `plugins.toml` as above.

## Hobserver (consumer) configuration

Nothing needs passing — the log defaults to `nemo-relay/atof/hermes-atof.jsonl`
under `$HERMES_HOME` when it is set, else under `~/.hermes`, which is the
directory the exporter settings above write to either way:

```bash
./hobserver
```

Override a source when it lives elsewhere by setting it for the tab in
`hobserver.toml`:

```toml
[[plugins]]
plugin = "plugins.turns"
settings = { atof_log = "/home/<you>/.hermes/nemo-relay/atof/hermes-atof.jsonl" }
```
