# `playbooks/` — how a box gets built

One Ansible project per **kind of box**, named after the doctype whose boxes its playbooks run
against. `grove.utils.ansible_project_dir("Gateway Server")` is literally
`playbooks/gateway_server`, so a folder name is not a convention you can rename freely.

```
playbooks/
  gateway_server/     gateway.yml  deploy_agent.yml  deploy_tls.yml  retire_exporter.yml  config.yml  memory.yml  stop.yml   + config.json.j2, systemd/
  ingress_server/     ingress.yml  deploy_agent.yml  config.yml
  inference_server/   provision.yml  serve.yml  reconfigure.yml  container_state.yml  teardown.yml  deploy_tls.yml
  machine/            ping.yml  grow_root.yml  scan_gpus.yml     — box-level, no role layered on yet
  monitoring_agent/   agent.yml  config.yml  exporters.yml  push_targets.yml  vector.yml
  gateway_store/      store.yml  restore.yml  stop.yml  ping.yml     — Redis only, shared by a Network's gateways; restore.yml loads a backup through a side Redis + REPLICAOF
  roles/              grove_user  remove_cloud_user  auditd  dcgm_exporter  node_exporter  fleet_tls  grove_https  install_gateway_agent  openresty  redis  redis_exporter  pathway_memory  vector
```

A project's own `roles/` is searched first, then `playbooks/roles/`. So a shared role is written once
and named from anywhere, with no copy and no symlink to keep pointing.

Some plays deliberately live in a project that is not the doctype they run for, because they are the
same work on either kind of box: `deploy_tls.yml` and `retire_exporter.yml` sit under `gateway_server/`
and an **Ingress** Server runs them with `project="Gateway Server"` (a standalone Inference Server has its own, because
its nginx needs a reload that pathway does not), and `exporters.yml` and `vector.yml` belong to Monitoring Agent but
run against the fleet's boxes (`exporters.yml` on Inference Servers, `vector.yml` on Gateway Servers,
Gateway Stores and Ingress Servers).

## How a play is actually run

Never from a shell. A doctype that stands on a box mixes in `AnsibleHost` and calls:

```python
self.run_playbook("deploy_agent.yml", extravars={...})
self.run_playbook("exporters.yml", project="Monitoring Agent")   # a shared play
```

Callers name a playbook — never a path, a server type or a Machine. `project=` is only for a play
that belongs to one doctype but runs against another's boxes.

`ansible_runner.run_play` then:

1. Loads the **Machine** behind the doc and refuses without a `public_ip`.
2. Builds a **single-host inventory** from it (`ssh_user`, `ssh_port`).
3. Pins `ansible_python_interpreter=/usr/bin/python3` when the Machine has a `cloud_provider`, because
   cloud GPU images ship several pythons and auto-discovery lands on one without `python3-apt`/cffi,
   which crashes the `apt` module. Extra-vars are highest precedence, and an explicit one still wins.
4. Runs Ansible's `PlaybookExecutor` **in-process** with a custom stdout callback, which is what
   writes the tracking docs live rather than after the fact.

There is no `ansible-playbook` subprocess in this path. (`Ansible.run()` exists for ad-hoc use and is
not what buttons call — it has no Frappe tracking.)

## What you read afterwards

- **Ansible Play** — one run, its status and `rc`. `rc == 0` is success; the callers turn that into
  Active or Broken.
- **Ansible Task** — one row per task, created **as it starts**. Handler tasks included: without that
  hook, a failure inside a handler produced a play that failed with no row saying why.

## Conventions in the plays themselves

- **Idempotent, and honest about it.** `creates:` guards on anything expensive (a generated key,
  predownloaded weights); a rendered script's *content* is what decides whether a container is
  replaced.
- **`extravars` write config files whole.** `agent.env` is generated from them, so a caller that omits
  one variable does not leave the old value — it writes a **blank**. That is fatal for the admin token
  and silently wrong for a hostname, which is why `tests/test_gateway_agent.py` asserts that every
  variable a template renders is one the button passes.
- **Restart vs reload is a real distinction.** Identity and secrets (`agent.env`) make it a different
  process → restart. Tunables (`config.json`) are re-read on `SIGUSR1` → reload, so live token streams
  are untouched. A new binary reloads too: `SIGHUP` forks a child that inherits the listening sockets
  and serves at once while the old process drains, so Deploy Agent is not a traffic event. A changed
  `agent.env` still restarts — the child reads its environment from systemd, not from the parent.
- **A certificate renewal is a file copy.** `deploy_tls.yml` ships the fleet wildcard and nothing else;
  pathway watches the file's mtime and uses the new one on the next handshake. The one restart is a box
  set up before its Geography had a certificate: pathway decides at startup whether to open :443, so the
  first certificate writes the TLS lines into `agent.env` and restarts once; every later run finds them.
- **`systemctl reload-or-restart`, not the systemd module's `state: reloaded`.** That refuses on an
  inactive unit — and a box with a stopped gateway is the likeliest one to be having a binary shipped
  to it, so the deploy would fail on the handler with the fix already on disk, unstarted.
- **Reconcile the *running* state, not just the file.** A `blockinfile` that already matches reports
  unchanged and never notifies its handler again, so a setting can sit correct in the config and inert
  in the process for the life of the box. See "enforce persistence on the running redis" in
  `roles/redis`, which a Gateway Store runs.
- **Pathway's memory cap is one number, in a drop-in.** `roles/pathway_memory` writes
  `pathway.service.d/memory.conf` with `MemoryMax`: the limit the Update Memory Limit button was
  given, or 0 for the box's RAM less a 512 MB reserve for everything else on it. Setup writes it
  too; on a live box the button runs `gateway_server/memory.yml` (Ingress Servers too), and the doc
  records the limit only once the play succeeds. The same file sets `GOMEMLIMIT` to 90% of the cap.
  `MemoryMax` lands on the running process at the `daemon-reload`; `GOMEMLIMIT` is environment, so
  it is read at pathway's next restart, which the role causes only when passed
  `pathway_memory_restart: true` — Resize Server does, on a box in maintenance with nothing in
  flight, because its RAM just changed.
- **Non-fatal cleanup is guarded, not assumed.** Stopping OpenResty on a box that never had it uses
  `failed_when: false`.
- **The binary is downloaded, never compiled.** The agent lives in its own repo; `install_gateway_agent`
  fetches a release with a `sha256:` checksum. Which release is a Grove Settings field, so a rollback
  is an edit plus a Deploy Agent — not a control-plane release. The one exception is a dev deploy:
  `scripts/dev_deploy_pathway.py` builds a pathway working tree into `<bench>/builds/` and ships it to
  one Gateway Server through the same play. `agent_version` still records the pinned release, and the
  next Deploy Agent or Setup puts that release back.

## Resizing a box

`Server Resize` (see `doctype/README.md`) runs the plays around a Machine stop/start. The two
`stop.yml`s stop the service through systemd — pathway's SIGTERM drains, redis's saves the RDB and
fsyncs the AOF — so the box is stopped idle and AWS's graceful-stop timer never races a drain. Neither
is `redis-cli SHUTDOWN`: the packaged unit has `Restart=always`. Both units are enabled and come back
on boot; `gateway_store/ping.yml` is the wait for redis to answer, `redis-cli ping` until `PONG`.
Pathway holds no usage to flush: each request lands in the store as it ends, or on the box's disk
spool when the store refuses.

## Restoring a Gateway Store

`gateway_store/restore.yml` loads one RDB into the live store without a restart: a side `redis-server`
(:6380, no password) reads it, the live one `REPLICAOF`s it for a few seconds, rewrites its own AOF from
what it got, and is a master again. Afterwards it drops every transient key (`inflight:*`, `health:*`,
`sticky:*`) and the `usage:<prefix>:<day>` buckets a drain newer than the snapshot already billed
(`drop_prefixes`), and raises each live key's `spent:<month>` to what Grove has billed it where the
snapshot has less (`spent_seeds`) — a snapshot older than the last pull would otherwise hand every key
the difference of its cap again. For a lost disk only: an AOF that survived already holds everything.

## Monitoring

One vmagent per fleet (`monitoring_agent/agent.yml`), on its own box. It holds no target list: it asks
Grove (`grove.monitoring.targets`, http_sd) for the boxes, engines and pods that name this agent and
re-asks on the interval, so a deploy or a new box appears with nothing re-run. With **Push Targets
Manually** ticked, Grove writes the lists onto the box instead (file_sd, `push_targets.yml`) for a Grove
the box cannot reach — same entries and labels (`machine`, `region`, `server`, `engine`), but a snapshot
that goes stale until Push Targets is pressed again. The agent box installs its own node_exporter, bound
to localhost, since no Setup play would, and scrapes itself.

Only inference boxes are scraped; a Gateway Server, Gateway Store or Ingress Server ships its host
metrics with Vector instead (below) and carries no exporter the fleet scrapes (a store's redis exporter
is bound to localhost and read by its own Vector). An inference box answers on one port and
its exporters are re-published as paths behind it (`/metrics/node`, `/metrics/gpu`), so it needs nothing
open but 22 and that port; the exporters still listen on 9100/9400, just not from outside. Every target
is https on the box's TLS front with a self-signed certificate, so both scrape jobs skip verification:
the scrape is encrypted, the box is not authenticated. Only the host job authenticates — the two
exporter paths sit behind basic auth (the Scrape Password, rotated by the Inference Server's Update
Scrape Auth button through `exporters.yml`), because an exporter answers anyone who reaches it. An
engine's `/metrics` under `/e/<slug>/` is guarded by nothing, exactly as when its port was open: vLLM's
auth middleware covers `/v1`, not `/metrics`.

### Vector → datum

Every Gateway Server, Gateway Store and Ingress Server runs Vector (`monitoring_agent/vector.yml`, shared role
`roles/vector`, pinned deb, the package's own unit). It pushes to **datum**, the write door in front of
ClickHouse, at the two routes its Region's Monitoring tab names: `host_metrics` plus Vector's own
`internal_metrics` by Prometheus remote write (`Metrics Endpoint`), and every
`/var/log/pathway/*.log` (`payload.log` included — the box's 7-day rotation is not the retention
story any more, datum's is; its lines run past Vector's 100 KiB default, so the file source reads up to 8 MiB a line, under datum's 12 MiB body) and `/var/log/redis/*.log` (Vector sits in `adm` for them) as datum log lines (`Logs Endpoint`: `ts`,
`product: grove`, `service` = log directory, `source` = file name; pathway's slog JSON is parsed, so
`level` and `message` are its `level` and `msg` and every other key is an attribute, groups flattened with `_`,
prompts and outputs whole, beside `host`, `server`, `machine`, `region`; a line that is not JSON keeps datum's
8 KiB message cap and ships whole as attribute `line`; a line a `vector_unshipped_lines` rule matches, by file, key and
values, stays on the box). Each box type names its own log files and rules in `vector_variables`: a pathway
box its three logs, keeping `access.log` lines with `path` `/healthz`; a store redis's log. Batches stay under datum's 10,000
cap and each sink flushes once a minute (datum takes 12 writes a minute per token and route), sink
healthchecks are off (datum answers 405 to a GET), and each sink keeps a disk buffer because datum
buffers nothing, dropping the newest event when it is full rather than stalling the sources. Host metrics arrive as `host_*` series, not node_exporter's `node_*` (cpu, disk, filesystem, load, host, memory,
network, tcp; not the per-process and per-cgroup collectors, which were four fifths of the volume; snap
loop devices and squashfs mounts and the loopback are excluded). On a Gateway
Store the same play lays oliver006's `redis_exporter` (`roles/redis_exporter`, localhost 9121, the store's
password from `vector_redis_password`, root-only env file, `DynamicUser` unit) and Vector scrapes it
(`prometheus_scrape`) into the same remote write as `redis_*` series: memory against maxmemory, clients,
evictions, ops, hit ratio, bgsave and AOF status. No password passed, no exporter and no scrape.

The token is datum's: a JWT Central signs for one `resource_id` (`grove:<machine>`), good for as long
as Central says (`expires_in`, 7 days today) with no revocation, addressed to the Region's `central_region`
(Central's own name for it). Grove mints it from Central (`grove.datum.mint_token`, Grove Settings → Central)
inside the job and stores only when it runs out (`datum_token_expires_at`); `grove.datum.refresh_tokens`,
hourly, re-runs the play on every box with `is_vector_setup` whose token has a day or less left. Setup chains the play as its own job; **Install Vector** (then **Update
Vector**) re-runs it by hand. A Gateway or Ingress Server set up before Vector shows **Migrate to Vector**
instead until `is_vector_setup`: vector.yml, then `gateway_server/retire_exporter.yml` stops and removes the
node_exporter and scrape htpasswd its old play installed — Vector first, so the box is never unwatched. `ship_host_metrics` off drops the `host_metrics` source and nothing else:
an Atlas machine's Pilot already ships those, and nobody else ships the logs.

datum keeps no roster, so a quiet `resource_id` looks the same whether the box died or Grove took it
down. Grove writes the roster itself, Atlas's way (`atlas_metal_server_info`): every minute
`grove.datum.push_roster` posts one `grove_machine_info{machine, status, vector} 1` per Machine under
`grove:region-<region>` to each Region's datum (the JSON route beside the remote-write one), `vector` 1 when a
box with `is_vector_setup` pushes under `grove:<machine>`. Reader rule (Insights): expected = Machines
whose latest roster row reads Active with vector 1; alert on one with no row of its own in the last few
minutes. Stopped and Terminated drop out of expected; an Active box that never shipped is in it, so a
broken pipeline shows. No per-action hook, so a new action (a resize job) needs nothing.

## Running one by hand

Prefer the doctype buttons: they pass the extra-vars, and they record what happened. When you do need
a shell, do it from the bench so the paths resolve, and pass the same extra-vars the button does —
notably `admin_token`, or the play asserts its way out rather than leaving a crash-looping box.
