# Setting up prompt timing

The Turns tab reads the ATOF JSONL stream that hermes-agent's bundled
`observability/nemo_relay` plugin exports (see docs/design/adr/0001 and 0002).
There are two halves: hermes-agent **produces** the log, hobserver
**consumes** it.

## Producing: enable the exporter in hermes-agent

1. **Install the NeMo Relay SDK** into the environment that runs
   hermes-agent. The WebUI runs the agent in-process, so for WebUI use this
   is the agent venv the WebUI points at:

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

   `~/.hermes/.env` is the right place because hermes-agent loads it itself
   at startup (`hermes_cli/env_loader.py`), so it reaches the agent no
   matter how it runs: the WebUI's in-process agent under any launcher
   (`ctl.sh`, `start.sh`, `bootstrap.py`) and plain `hermes chat` from a
   terminal. Restart the process after editing — the file is read once at
   startup.

   The directory is created automatically. The default filename is
   `hermes-atof.jsonl` (`HERMES_NEMO_RELAY_ATOF_FILENAME` overrides it).
   Keep `append` mode — hobserver's tailer reads incrementally and
   treats a shrinking file as a rotation, but append is the mode ADR 2
   assumes day to day.

4. **Verify** with a one-shot CLI run:

   ```bash
   hermes chat -q "Reply exactly ok"
   wc -l ~/.hermes/nemo-relay/atof/hermes-atof.jsonl
   ```

   A missing or empty file means the plugin is not enabled, the SDK is not
   installed in that venv, or the env vars are not reaching the process —
   the plugin **fails open** and reports none of these.

## Consuming: point hobserver at the log

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
