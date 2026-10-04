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
| **Gateway Server** | groups, users, keys, the global route table | yes |
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

## Where each concern lives

| File | Owns |
|---|---|
| `pathway/run.py` | What every run shares: `Target` (one box's admin API), `SyncRun` (lock, parallel dial, rows in the order asked). Every path that reaches a box goes through it. |
| `pathway/projection.py` | `Projection`: every push to every box. A push that left no Pathway Sync row did not happen. |
| `pathway/usage.py` | `Usage`: each store's drain (or one user's keys) into one Usage Record per key (two when it holds both billed and free usage), then the ack of what landed. `reconcile.py` lands each touched user: records, `spent` moved by Grove's price, the gateway's charge audited, the verdict settled. |
| `billing/` | Usage counters, sell prices, the credit ledger, usage records and the Revenue report — its own Frappe module, [`billing/README.md`](billing/README.md). |
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
| `api.py` | The whitelisted surface a customer's portal calls. |
| `net.py` / `utils.py` | Addresses, slugs, paths. |

## What gets pushed, and under which key

The agent's admin API is token-gated (`X-Grove-Admin-Token`) at
`<box>/grove-admin/{state,state-hash,usage}`. The push is **desired state, whole, and absence
prunes**: `POST state` carries any subset of the four sections (groups, users, keys, routes),
each stamped with a hash the agent stores in `grove:state_hash` and returns from `GET state-hash`.
The tick pushes only sections whose hash the box does not already hold; a wiped Redis holds no
hashes, so the next tick re-pushes everything — that IS the repair path. `users` and `keys` are
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
        users:ef dd44…          users:ef dd44…     same → skip

  minted key hashes into ONE bucket → one ~25 KB push, not the population.
  wiped Redis → held column empty → every row differs → full re-push next tick.
```

Every builder sorts by an immutable unique id (`key_hash`, doc name, deployment id) before
hashing — same DB state must serialize identically whatever order the query returns, or the
fleet gets re-pushed over row order. Order means nothing on the wire; it exists for the hash.

| Redis key | Written by | Holds |
|---|---|---|
| `key:<sha256(secret)>` | state push (keys) | whose the key is |
| `user:<name>` | state push (users) + the agent | groups (comma list), own allow/deny, `limited`, `budget` (the amount loaded); the agent's own lifetime `spent` |
| `model_group:<name>` | state push (groups) | the model grant for everyone in it |
| `deploy:<model>` | state push (routes) | every placement of one model |
| `grove:state_hash` | state push | per-section/bucket hashes of what the box holds |
| `usage:<prefix>` | the agent | live counters: `m:<metric>:<model>` for reports, `p:<pricing>:<counter>` and `p:<pricing>:cost` for billing, the same under `f:` for what was served free |
| `drained:<drain>:<prefix>`, `drain:unacked` | the agent's drain | a key's counters set aside under a drain id, re-sent until Grove acks that pair; kept for `usage_retention` after |
| `sticky:<session>` | the agent | session → engine, for prefix-cache reuse |
| `inflight:<engine>` | the agent | what is running right now |
| `health:<target>` | the agent | consecutive failures behind passive ejection (60s TTL) |

Access is pushed as **three** records, one per thing that can change on its own: a group edit is one
record however many members, a budget flip is one record however many keys, and the agent resolves
all three at request time — unioning every group the user names before applying their own
allow/deny.

A model id is always `<provider>/<name>` (`frappe/qwen3.5-4b`) — the Model's **key**, not its doc
name, which is a hash. One key, one route key, one grant — the bare form was deliberately broken,
because routing keys on `deploy:<key>` while access is matched against the string the caller *sent*,
so a route key with no matching grant is a 403 before routing is ever consulted. A key may be several
docs, one per provider record: `openai/gpt-5` under the `in` record and under the `eu` record are two
Models, each with its own upstream id and pricing, and a gateway's table carries only its own
geography's. A Link (pricing, replica, grant row) names a doc; everything on the wire — routes,
grants, usage buckets, `available_models` — names the key. A grant is by key, so it reaches the id
wherever a geography serves it; availability is by geography.

The provider is not only a prefix. Give a `Model Provider` a Base URL and a key and its published
models route straight to that vendor — a `kind: "provider"` row naming the provider as the placement,
with no deployment, no pod and no capacity of ours to divide. The record's **Keys** table rides the
row as `credentials` (`[{id, secret}]`, the id being the key row's name, in table order) with the
record's **Key Selection** (`round_robin`); `internal_key` stays blank on a provider row and is the
single-key spelling only an engine row uses. The gateway takes the keys in turn — one cursor per
vendor across all its models, so the limits the keys share are drawn on evenly — swaps a key the
vendor answers 429 for the next (each tried once, then once more round), skips one answering
401/402/403 for the rest of that request, and counts what every key answered. **Key Stats** on the
form reads those counts live off one gateway per store in the geography; nothing is stored here.
**Fetch Models** asks the vendor's `/v1/models` on one front — the OpenAI one with a Bearer when set
(a dual vendor's Anthropic shim, DeepSeek's, has no list), else Anthropic's with `x-api-key` and the
version header, paged with `after_id` — hides what the record already holds, and adds the ticked ids
as unpublished Models, the vendor's spelling kept as `Upstream Model ID` where ours differs
(`org/Model` → `org-model`).
What the vendor is *asked* for is the route's `upstream_model`, which the control plane computes in
one place (`_upstream_model`):

| | sent upstream |
|---|---|
| `Upstream Model ID` set on the Model | that string, whatever the route kind — the only thing that reaches a container image advertising its own name |
| a vendor, no override | the bare id (`claude-4-5`) — our namespace is not theirs |
| anything we run | **nothing** — blank, because the engine's `--served-model-name` IS the full Grove id |

Access, routing, metering and `/v1/models` all key on the id the caller sent, so the rewrite never
desyncs a grant from a route, and usage lands against the Grove model whatever the vendor calls it.

## Prices and credits

Live in [`billing/`](billing/README.md): the counter table, sell prices, the prepaid balance, what a
drain is audited for, and the Revenue report.

## The catalog (`catalog/`)

Providers, vendor models and their prices ship in `catalog/catalog.json`, not in `fixtures/`: a
migrate deletes and re-inserts every fixture doc, which wipes its API key and resets what the
operator set. The file is authored, not dumped from a site: the counter table, `Main`, and the
vendors we sell — openai (`gpt-6-luna`, `gpt-6-sol`), anthropic (`claude-sonnet-5-5`), baseten
(`deepseek-v4.1-flash`; DeepSeek is bought through Baseten, not direct) — at the vendors' list
prices of 2026-10-03 — and the fleet skeleton: the `aws` account and `ap-south-1`.

- `seed.insert_missing()` runs after install and after every migrate. It inserts a geography,
  cloud account (keyless, for the operator to fill), region or model the site has no doc
  named for, and a provider the site has no record named for in any geography. It never updates
  and never deletes.
- A geography lands with `endpoint` and `fleet_zone` blank, for the operator to fill, and is the
  default only when the site has none. Until then a box in it serves :80 in the clear and
  `api.provision_key` refuses it.
- A model group the site has no doc named for lands with the models the file lists for it, by key,
  in the geography those models landed in, and is that geography's default only when it has none.
  The file ships one, `catalog`, granting every model in it. A model added to the file later does
  not join a group the site already holds.
- Pricing is loaded per model, by hand: **Load Pricing** on the Model form (`Model.load_pricing`)
  inserts the catalog's rates as a Disabled `Model Pricing`. Shown while the catalog prices the
  model and no pricing names it. Enabling and publishing stay the operator's call.
- `bench --site <site> execute grove.catalog.export.write` rewrites the file from the site: every
  provider once (no API key, a vendor's geography set to `Main`), vendor models only,
  each with the rows of its Enabled pricing, the one geography `Main`, every cloud account's type
  (no key), every region, and every model group with the exported models it grants (no `is_default`, no geography).

## Scheduled jobs (`hooks.py`)

| When | Job | Note |
|---|---|---|
| `*/1` | `pathway.projection.sync_projection` | hash-gated: pushes each box only what it does not already hold; a fleet in sync logs nothing. Also sends each store the spend adjustments it is owed (pending `Credit Discrepancy` rows) |
| `*/2` | `cloud_provider.reconcile.sync_all` | the provider owns whether a pod is up; this closes the drift |
| `*/5` | `gateway_store.backup_all` | one `redis-cli --rdb` snapshot per Active store, over SSH, into the weights bucket under `gateway-store/<store>/<utc stamp>.rdb`, the doc's Last Backup Key pointing at it; usage leaves a store only on the hourly pull, so this is what a lost disk costs: 5 minutes. Off until the bucket and Mirror keys are set; prune with a bucket lifecycle rule |
| hourly | `pathway.usage.pull_all` | a store is drained once, through its first writer that answers; recorded, billed, then what landed is acked. A failed pull loses nothing: the box re-sends every unacked pair. Also Gateway Server → **Pull Usage**, Stuck Usage → **Pull Now**, and `api.pull_usage(email)` for one user (3 an hour per user, counted in the site cache; the rest are not limited) |
| hourly | `tls.renew_fleet_certificate`, `cloud_provider.schedule.run_due_pods` | |

Nothing else pushes. A doctype hook, a provision and a pod lifecycle all just write state; the
tick carries it within a minute. The only manual paths are Gateway Server → **Full Sync**, Ingress
Server → **Sync Replicas**, and **Force Sync All** on the Pathway Sync list.

There is no separate backstop job: the hashes live on the box, so losing the store means losing
them, which the very next tick reads as drift and heals. All Projection runs serialize on one
MariaDB advisory lock, so a slow run cannot land a stale write after a newer one.

**Boxes are dialled in parallel** (`MAX_PARALLEL = 8`), a store's writers in turn, so one dead box
costs a run one timeout, not one per box. Rows land in the order asked, whichever box answered
first. The rule for anyone adding a run (`SyncRun`): resolve on the main thread (`units()`,
`Target.resolve`), HTTP only on the pool (`work()`: `Target.dial`, `in_turn`), record on the main
thread (`settle()`) — a pool thread has no `frappe.local`, so no db, no docs, no `frappe.throw`. A usage drain is recorded the
moment it arrives; one that cannot be recorded fails its own row and acks nothing, so the box sends
it again.

A quiet tick still leaves a trace on an Ingress Server: every in-sync check and successful push
stamps its `last_synced_at`, and a stale stamp means the box is unreachable or rejecting pushes.
Gateways carry no stamp — the Pathway Sync rows are their record.

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
- **A new line in `modules.txt` needs `bench clear-cache` before `migrate`.** The module map is
  cached in Redis; `sync_module_defs` reads the file and makes the Module Def, but `sync_for` walks
  the cached map and never sees the new folder, so its doctypes keep their old module until the
  next migrate. Clear first, or migrate twice.
- **A Single doctype never applies its JSON default** if it predates the field. Blank is a state a
  real site lands in, so a new setting either has a safe blank meaning or throws (see
  `fleet.gateway_agent_version`).
- **A saved `Password` field reads back truthy** — asterisks in the doc's own column, the value in
  `__Auth`. Test through `get_password`, never `if not self.field`.
- **`db_set` skips `validate` and fires no `on_update`**, which is why status writes use it — and why
  a path that writes status must then do by hand whatever `on_update` would have done.
- **Ansible swallows whatever a callback raises**, logging `Failure using method (…)` and running
  on. So a play's own docs are written best-effort: each write carries Frappe's
  `dangerously_reconnect_on_connection_abort` (Ansible forks a worker per task, and a child closing
  the inherited socket takes this process's connection with it), and the rc a caller acts on is
  Ansible's, never the doc's status.
- **Deploy the agent before the state that needs it.** An older binary reading a newer projection
  fails in whichever direction that field's blank means: `models` fails *closed* (403 everyone), an
  unknown route `kind` fails *open* (wrong dial).
