# `grove/` — the control plane

Grove is a Frappe app that **owns infrastructure state and tenancy**, and a Go agent
(`pathway`, its own repo) that **serves inference traffic**. This app never sits on a request
path. It provisions boxes, and it projects state onto them.

Read this with [`../CLAUDE.md`](../CLAUDE.md) (the rules) and the per-directory READMEs:
[`grove/doctype/`](grove/doctype/README.md) · [`billing/`](billing/README.md) · [`cloud_provider/`](cloud_provider/README.md) ·
[`playbooks/`](playbooks/README.md) · [`tests/`](tests/README.md).

## The one rule everything follows

**Grove is the source of truth; a box holds a projection it never edits.** Anything a gateway
originates is only usage counters, which is why gateway Redis runs AOF (`appendfsync everysec`: a
crash loses at most a second of counts). Everything else can be pushed again, so nothing is ever
read back out of a box to decide what is true.

## Two planes

| | takes | holds tenant state |
|---|---|---|
| **Gateway Server** | groups, keys, the global route table | yes |
| **Ingress Server** | one thing: the replica table for the boxes in its own Network | no |

A gateway's Redis is its Network's **Gateway Store**: Setup puts a new gateway on the Network's
Active store and every deploy keeps it there (`Gateway Server.gateway_store` records which).
Gateways on one store share `inflight:<engine>`, so a standalone box they all dial directly is capped
once across them rather than once per gateway. They share everything else too: a dead store fails
its gateways closed. Gateways on different stores still count apart. **Maintenance** (a
`config.json` key: new requests 503, running ones finish, `GET /grove-admin/in-flight` counts them)
is how a gateway is drained before anything restarts it.

The split is enforced by what each is *given*, not by a flag: an Ingress Server doctype has no
tenant fields, and the agent in ingress mode mounts no endpoint to send them to. A box behind an
ingress contributes a route row that names the ingress and never its own address, so replica
topology stays inside its VPC and several deployments behind one ingress fold into **one** row.
The `capacity` on that folded row is the sum of its replicas' and is advisory: the gateway reads it
only to choose *between* ingresses. The ingress applies the exact per-replica gate, which is why two
gateways cannot jointly overrun one replica.

## Where each concern lives

| File | Owns |
|---|---|
| `pathway/run.py` | What every run shares: `Target` (one box's admin API), `SyncRun` (lock, parallel dial, rows in the order asked). Every path that reaches a box goes through it. |
| `pathway/projection.py` | `Projection`: every push to every box. A push that left no Pathway Sync row did not happen. |
| `pathway/usage.py` | `Usage`: each store's drain (or one team's keys) into one Usage Record per key, then the ack of what landed. `reconcile.py` lands each touched team: records, `spent` moved by Grove's price, the gateway's charge audited, the team settled. |
| `billing/` | Usage counters, sell prices, monthly caps, usage records, the Revenue report and the Vendor Reconciliation report — its own Frappe module, [`billing/README.md`](billing/README.md). |
| `catalog/` | What a site starts with: `catalog.json` (geographies, providers, vendor models and their rates, model groups; no secret, no `published`), `seed.insert_missing` (after install and after every migrate), `export.write` (by hand). |
| `pathway/snapshot.py` | The desired state a box is pushed, and the hash gate that decides which sections travel. |
| `pathway/routes.py` | `deploy:<model>` tables — a gateway's for its Geography, an ingress's for the boxes it owns. |
| `access.py` | Which models a user may call, as the CSV each grant record carries. |
| `serving/` | One class per engine kind: what starts it, what environment it needs, what proves it serves. |
| `fleet.py` | What a named fleet box (Gateway/Ingress) does the same way, plus the fleet-wide settings readers. |
| `naming.py` | A Machine's `<prefix><n>-<region>.<geography zone>`, e.g. `gw2-ap-south-1.local.frappe.dev` (no domain for a box whose geography has no zone, or one named before this), which its server doc takes. Its `short_name` (first label) is the DNS label under the zone and the request-id / agent id. |
| `ansible.py` / `ansible_runner.py` | Running a playbook against a box, tracked as docs. |
| `tls.py` | Each Geography's zone wildcard: issue over DNS-01, renew, push to that geography's boxes. |
| `monitoring.py` / `log_relay.py` | Exporters on every box, and shipping their output. |
| `failure.py` | `@reports_failure` — a long job that dies marks its doc Broken and says why. |
| `api/` | The whitelisted surface Central calls, one module per subject: `control` (enrollment), `teams`, `keys`, `catalog` (geographies, models), `usage` (usage, figures, pulls). |
| `net.py` / `utils.py` | Addresses, slugs, paths. |

## What gets pushed, and under which key

The agent's admin API is token-gated (`X-Grove-Admin-Token`) at
`<box>/grove-admin/{state,state-hash,usage}`. The push is **desired state, whole, and absence
prunes**: `POST state` carries any subset of the three sections (groups, keys, routes),
each stamped with a hash the agent stores in `grove:state_hash` and returns from `GET state-hash`.
The tick pushes only sections whose hash the box does not already hold; a wiped Redis holds no
hashes, so the next tick re-pushes everything — that IS the repair path. `keys` is
split into 256 buckets (`pathway.snapshot.bucket_of`) hashed independently, so one key minted re-pushes
one bucket, not the population. The full contract lives in `plan_agent_state_sync.md` at the
repo root.

```
every minute            Grove                                    box (pathway + Redis)
                          │                                        │
  build snapshot,         │──── GET /grove-admin/state-hash ──────▶│
  hash each section       │◀··· hashes the box holds ··············│
  and bucket              │                                        │
                          │  compare — all equal? stop. no log.    │
                          │                                        │
                          │──── POST /state (drift only) ─────────▶│  one MULTI:
                          │                                        │  upsert named, DEL unnamed,
                          │                                        │  store hashes — or 500 and
                          │                                        │  nothing lands
```

The compare is per fingerprint, so wire cost tracks what changed, not fleet size:

```
        desired (computed)      held (grove:state_hash)
        groups   aa11…          groups   aa11…     same → skip
        routes   bb22…          routes   bb22…     same → skip
        keys:3f  7d90…          keys:3f  9f3a…     DIFFERS → ship bucket 3f only
        keys:ef  dd44…          keys:ef  dd44…     same → skip

  minted key hashes into ONE bucket → one ~25 KB push, not the population.
  wiped Redis → held column empty → every row differs → full re-push next tick.
```

Every builder sorts by an immutable unique id (`key_hash`, doc name, deployment id) before
hashing — same DB state must serialize identically whatever order the query returns, or the
fleet gets re-pushed over row order. Order means nothing on the wire; it exists for the hash.

| Redis key | Written by | Holds |
|---|---|---|
| `key:<sha256(secret)>` | state push (keys) + the agent | the whole policy: `team`, `group` (comma list), own `allow`/`deny`, `geography`, `limits` (the key's own, else its team's Tier's, else the default tier's, else the defaults), `budget` (its monthly cap), `limited`, `log_payloads`; the agent's own `spent:<YYYY-MM>`, this month's spend |
| `model_group:<name>` | state push (groups) | the model grant for everyone in it |
| `deploy:<model>` | state push (routes) | every placement of one model |
| `grove:state_hash` | state push | per-section/bucket hashes of what the box holds |
| `usage:<prefix>:<YYYY-MM-DD>` | the agent | a key's live counters for one UTC day it was served, the `day` its Usage Record carries: `m:<metric>:<model>` for reports, `p:<pricing>:<counter>` and `p:<pricing>:cost` for billing |
| `drained:<drain>:<prefix>:<day>`, `drain:unacked` | the agent's drain | a key's day bucket set aside under a drain id, re-sent until Grove acks that pair; kept for `usage_retention` after |
| `sticky:<session>` | the agent | session → engine, for prefix-cache reuse |
| `inflight:<engine>` | the agent | what is running right now |
| `health:<target>` | the agent | consecutive failures behind passive ejection (60s TTL) |

Access is pushed as **two** records: a group edit is one record however many members, and the key
carries everything else — the agent resolves the two at request time, unioning every group the key
names before applying its own allow/deny. The team is not a record on the box: what is the team's
(payload logging, suspension) is stamped on each of its keys, so suspending the team is one
field per key.

A model id is always `<provider>/<name>` (`frappe/qwen3.5-4b`) — the Model's **key**, not its doc
name, which is a hash. One key, one route key, one grant — the bare form was deliberately broken,
because routing keys on `deploy:<key>` while access is matched against the string the caller *sent*,
so a route key with no matching grant is a 403 before routing is ever consulted. A key may be several
docs, one per provider record: `openai/gpt-5` under the `in` record and under the `eu` record are two
Models, each with its own upstream id and pricing, and a gateway's table carries only its own
geography's. A Link (pricing, replica, grant row) names a doc; everything on the wire — routes,
grants, usage buckets, `available_models` — names the key. A grant is by key, so it reaches the id
wherever a geography serves it; availability is by geography.

What a model takes and gives is two multi-selects on the Model, **Input Modalities** and **Output
Modalities**, each a list of `Modality` records (`Text`, `Image`, `Audio`, `Video`, `File`; `Text`,
`Image`, `Embeddings`, `Transcription` for a speech-to-text model's transcript), each marked as an
input, an output or both, seeded from the catalog. A record's name starts with a capital, however
it was typed; the gateway, the engine and `available_models` get it lowercased (`text`). A list
left blank is `Text`.
There is no single `modality` word any more: everything that read it reads the lists. Every row of
`deploy:<key>` carries both as `input_modalities` and `output_modalities`; the gateway reads which
surfaces the model answers on off the outputs (`text` for chat, `embeddings` for `/v1/embeddings`,
`transcription` for `/v1/audio`); the inputs it only carries. `available_models` answers
the two lists per model. An engine we launch is `--language-model-only` when it is text in and
text out, and a pooling model when it gives `embeddings`.

The provider is not only a prefix. Give a `Model Provider` a Base URL and a key and its published
models route straight to that vendor — a `kind: "provider"` row naming the provider as the placement,
with no deployment, no pod and no capacity of ours to divide. The record's **Keys** table rides the
row as `credentials` (`[{id, secret}]`, the id being the key row's name, in table order) with the
record's **Key Selection** (`round_robin`); `internal_key` stays blank on a provider row and is the
single-key spelling only an engine row uses. The row also carries `denied_tools`: every `Denied Tool`
row under the vendor's Provider Name, sorted — a `tools[].type` or a top-level request field the
vendor would run on its own side and bill outside the token counts, which nothing meters. The gateway
answers a request that names one with a 400 before the dial (`route`, from the client's own bytes),
so a search or a code run never reaches the vendor. One doctype for all geographies, keyed by the
name and not by a Model Provider link, because the list is the vendor's. Always written, so a row
deleted here is a change the next tick pushes; delete the row once a tool is priced, and drop it from
the catalog too or the next migrate puts it back. The gateway takes the keys in turn — one cursor per
vendor across all its models, so the limits the keys share are drawn on evenly — swaps a key the
vendor answers 429 for the next (each tried once, then once more round), skips one answering
401/402/403 for the rest of that request — an exhausted account reported under a 400 or 429
(Anthropic's "credit balance", OpenAI's `insufficient_quota`) counts as the 402 it is — and answers
such a refusal with its own 502 (`upstream credential rejected` / `upstream account
unpaid` / `upstream credential lacks permission`), never the vendor's words, which go to its error
log. It counts what every key answered: `requests`, `ok`, `rate_limited`, `unauthorized`, `unpaid`,
`forbidden` (`rejected` is those three together), `failed`, `last_used`, `last_rate_limited`. Every 6
hours `grove.pathway.key_stats.pull_all` reads them off every store into **Provider Key Stats**, one row
per key per store with what it answered since the pull before, in one `stats` JSON field (the
`last_*` seconds as site time; a key the store did not dial leaves no row). The gateway clears the
counters as it answers, so a pull that does not land loses those 6 hours. Kept 90 days.
**Fetch Models** asks the vendor's `/v1/models` on one front — the OpenAI one with a Bearer when set
(a dual vendor's Anthropic shim, DeepSeek's, has no list), else Anthropic's with `x-api-key` and the
version header, paged with `after_id` — hides what the record already holds, then asks for our id
for each ticked one (prefilled with the vendor's) and adds them as unpublished Models, the vendor's
spelling kept as `Upstream Model ID` where ours differs (`org/Model` → `org-model`).
What the vendor is *asked* for is the route's `upstream_model`, which the control plane computes in
one place (`_upstream_model`):

| | sent upstream |
|---|---|
| `Upstream Model ID` set on the Model | that string, whatever the route kind — the only thing that reaches a container image advertising its own name |
| a vendor, no override | the bare id (`claude-4-5`) — our namespace is not theirs |
| anything we run | **nothing** — blank, because the engine's `--served-model-name` IS the full Grove id |

Access, routing, metering and `/v1/models` all key on the id the caller sent, so the rewrite never
desyncs a grant from a route, and usage lands against the Grove model whatever the vendor calls it.

## Prices and caps

Live in [`billing/`](billing/README.md): the counter table, sell prices, the monthly caps, what a
drain is audited for, the Revenue report, and the Vendor Reconciliation report that sets what Grove
charged beside what the vendor billed.

## The catalog (`catalog/`)

Providers, vendor models and their prices ship in `catalog/catalog.json`, not in `fixtures/`: a
migrate deletes and re-inserts every fixture doc, which wipes its API key and resets what the
operator set. The file is authored, not dumped from a site: the counter table, `Main`, and the
vendors we sell — openai (`gpt-6-luna`, `gpt-6-sol`), anthropic (`claude-sonnet-5-5`), baseten
(`deepseek-v4.1-flash`; DeepSeek is bought through Baseten, not direct) — at the vendors' list
prices of 2026-10-03 — and the fleet skeleton: the `aws` account and `ap-south-1`.

- `seed.insert_missing()` runs after install and after every migrate. It inserts a geography,
  cloud account (keyless, for the operator to fill), region or model the site has no doc
  named for, a provider the site has no record named for in any geography, and a `Denied Tool`
  pair the site lacks. It never updates and never deletes.
- `denied_tools` is what a vendor bills outside the token counts, and only that: Anthropic's web
  search ($10 per 1,000 searches) and code execution (container hours), OpenAI's `web_search_options`
  and the Responses API's priced hosted tools (web search, file search, code interpreter, image
  generation). A server tool priced in tokens alone — Anthropic's web fetch, tool search and MCP
  connector, OpenAI's `mcp` — is not listed and goes through; so does a client-run tool. DeepSeek's
  Anthropic front runs web search (probed 2026-10-09: `server_tool_use` blocks came back) and its
  price list names no rate for it, and Baseten charges for its tools, so both carry the two web
  search types.
- A geography lands with `endpoint` and `fleet_zone` blank, for the operator to fill, and is the
  default only when the site has none. Until then a box in it serves :80 in the clear and
  `api.keys.provision_key` refuses it.
- A model group the site has no doc named for lands with the models the file lists for it, by key,
  in the geography those models landed in, and is that geography's default only when it has none.
  The file ships one, `catalog`, granting every model in it. A model added to the file later does
  not join a group the site already holds.
- Pricing is loaded per model, by hand: **Load Pricing** on the Model form (`Model.load_pricing`)
  inserts the catalog's rates as a Disabled `Model Pricing`. Shown while the catalog prices the
  model and no pricing names it. Enabling and publishing stay the operator's call.
- `bench --site <site> execute grove.catalog.export.write` rewrites the file from the site: every
  provider once (no API key, a vendor's geography set to `Main`), every denied tool, vendor models only,
  each with the rows of its Enabled pricing, the one geography `Main`, every cloud account's type
  (no key), every region, and every model group with the exported models it grants (no `is_default`, no geography).

## Scheduled jobs (`hooks.py`)

| When | Job | Note |
|---|---|---|
| `*/1` | `pathway.projection.sync_projection` | stamp-bounded and hash-gated: rebuilds only the key buckets a save moved since the stores' `last_synced_at`, reads every box's hashes, pushes each box only what it does not already hold; a fleet in sync logs nothing and builds no key record. Every 10th minute every bucket is rebuilt. Also sends each store the spend adjustments it is owed (Pending `Gateway Spend Adjustment` rows) |
| `*/2` | `cloud_provider.reconcile.sync_all` | the provider owns whether a pod is up; this closes the drift |
| `0 */6` | `pathway.key_stats.pull_all` | one Provider Key Stats row per key per store: what it answered since the last pull, cleared on the gateway as it is read |
| `*/5` | `gateway_store.backup_all` | one `redis-cli --rdb` snapshot per Active store, over SSH, into its Geography's backup bucket (Store Backups tab) under `gateway-store/<store>/<utc stamp>.rdb`, the doc's Last Backup Key pointing at it; usage leaves a store only on the hourly pull, so this is what a lost disk costs: 5 minutes. Skipped in a Geography with no backup bucket; Restore never crosses geographies; prune with a bucket lifecycle rule |
| hourly | `pathway.usage.pull_all` | a store is drained once, through its first writer that answers; recorded, billed, then what landed is acked. A failed pull loses nothing: the box re-sends every unacked pair. Also Gateway Server → **Pull Usage**, Stuck Usage → **Pull Now**, and `api.usage.pull_usage(team)` for one team, which answers its `figures` afresh (2 an hour per team, counted in the site cache; the rest are not limited) |
| hourly | `tls.renew_fleet_certificate`, `cloud_provider.schedule.run_due_pods` | renew runs certbot only when it says the certificate is due, and pushes only when the certificate changed |

Nothing else pushes. A doctype hook, a provision and a pod lifecycle all just write state; the
tick carries it within a minute. The only manual paths are Gateway Server → **Full Sync**, Ingress
Server → **Sync Replicas**, and **Force Sync All** on the Pathway Sync list.

Grove Settings → **Pause Projection** stops the scheduled push; **Pause Usage Pull** stops the
hourly usage pull and 6-hourly key stats read (`SyncRun.pause_field`). The manual paths above still work.

There is no separate backstop job: the hashes live on the box, so losing the store means losing
them, which the very next tick reads as a box with no key hash and rebuilds everything for. All
Projection runs serialize on one MariaDB advisory lock, so a slow run cannot land a stale write
after a newer one.

**Boxes are dialled in parallel** (`MAX_PARALLEL = 8`), a store's writers in turn, so one dead box
costs a run one timeout, not one per box. Rows land in the order asked, whichever box answered
first. The rule for anyone adding a run (`SyncRun`): resolve on the main thread (`units()`,
`Target.resolve`), HTTP only on the pool (`work()`: `Target.dial`, `in_turn`), record on the main
thread (`settle()`) — a pool thread has no `frappe.local`, so no db, no docs, no `frappe.throw`. A usage drain is recorded the
moment it arrives; one that cannot be recorded fails its own row and acks nothing, so the box sends
it again.

### The projection tick

Every minute, bounded by a stamp. Each Gateway Store, each gateway not yet on one and each Ingress
Server carries `last_synced_at`: the read time of the last tick it took a push from or already
held. A stale stamp means the box is unreachable or rejecting pushes; the failed Pathway Sync rows
say why.

**A store is reached through its writers.** Each tick (and each usage pull) reaches a gateway not
yet on a store directly, and a Gateway Store through the gateways marked **Gateway Store
Writer**, tried in name order until one succeeds — every failed attempt still writes its row.
Other gateways on the store are never pushed: they read what the writer wrote. A store with Active
gateways but no Active writer writes a failed row naming the store; nothing is handed over
automatically. The first gateway set up on or moved onto a store is marked for you.

## Gotchas worth knowing before you touch something

- **One bad user holds only themselves back.** Each touched user lands in its own savepoint. A user
  whose share cannot be recorded (an unknown pricing id, a priced counter this Grove does not know
  because the gateway is newer, a bug) is rolled back alone and left unacknowledged, so the gateway
  keeps it and re-sends it every pull; everyone else is acked. Grove
  logs it as one open **Stuck Usage** row per user and store — keys, first and last failure,
  attempts, last error and payload — updated each failing pull and resolved by the pull that lands
  it. **Pull Now** on the row pulls just that user. The usage itself lives only on the gateway
  until then, so a Redis flush loses it; Log Settings clears resolved rows after 90 days.
- **Ansible swallows whatever a callback raises**, logging `Failure using method (…)` and running
  on. So a play's own docs are written best-effort: each write carries Frappe's
  `dangerously_reconnect_on_connection_abort` (Ansible forks a worker per task, and a child closing
  the inherited socket takes this process's connection with it), and the rc a caller acts on is
  Ansible's, never the doc's status.
