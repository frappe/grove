# `grove/` — the control plane

Grove is a Frappe app that **owns infrastructure state and tenancy**, and a Go agent
(`pathway`, its own repo) that **serves inference traffic**. This app never sits on a request
path. It provisions boxes, and it projects state onto them.

Read this with [`../CLAUDE.md`](../CLAUDE.md) (the rules) and the per-directory READMEs:
[`grove/doctype/`](grove/doctype/README.md) · [`cloud_provider/`](cloud_provider/README.md) ·
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
| `pathway/usage.py` | `Usage`: each store's drain (or one user's keys) into one Usage Record per key, then the ack of what landed. `reconcile.py` lands each touched user: records, `spent` moved by Grove's price, the gateway's charge audited, the verdict settled. |
| `pricing.py` | Prices per counter (`PriceBook`: by the pricing id the gateway charged, or by day for older records), the prepaid balance, `settle` (the one writer of `balance` and `credit_exhausted`). |
| `grove/report/revenue/` | Revenue report: what each drain was billed, cut by model, API key, user or day. |
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
| `usage:<prefix>` | the agent | live counters: `m:<metric>:<model>` for reports, `p:<pricing>:<counter>` and `p:<pricing>:cost` for billing |
| `drained:<drain>:<prefix>`, `drain:unacked` | the agent's drain | a key's counters set aside under a drain id, re-sent until Grove acks that pair; kept for `usage_retention` after |
| `sticky:<session>` | the agent | session → engine, for prefix-cache reuse |
| `inflight:<engine>` | the agent | what is running right now |
| `health:<target>` | the agent | consecutive failures behind passive ejection (60s TTL) |

Access is pushed as **three** records, one per thing that can change on its own: a group edit is one
record however many members, a budget flip is one record however many keys, and the agent resolves
all three at request time — unioning every group the user names before applying their own
allow/deny.

A model id is always `<provider>/<name>` (`frappe/qwen3.5-4b`). One id, one route key, one grant —
the bare form was deliberately broken, because routing keys on `deploy:<id>` while access is matched
against the string the caller *sent*, so a route key with no matching grant is a 403 before routing
is ever consulted.

The provider is not only a prefix. Give a `Model Provider` a Base URL and a key and its published
models route straight to that vendor — a `kind: "provider"` row naming the provider as the placement,
with no deployment, no pod and no capacity of ours to divide. What the vendor is *asked* for is the
route's `upstream_model`, which the control plane computes in one place (`_upstream_model`):

| | sent upstream |
|---|---|
| `Upstream Model ID` set on the Model | that string, whatever the route kind — the only thing that reaches a container image advertising its own name |
| a vendor, no override | the bare id (`claude-4-5`) — our namespace is not theirs |
| anything we run | **nothing** — blank, because the engine's `--served-model-name` IS the full Grove id |

Access, routing, metering and `/v1/models` all key on the id the caller sent, so the rewrite never
desyncs a grant from a route, and usage lands against the Grove model whatever the vendor calls it.

## Prices and credits (`pricing.py`)

Money is decided in the control plane. The gateway holds its own copy — rates per pricing on its
routes, the amount each user loaded on their record, its own spend counter — and gates on it. Grove
keeps its own balance, and every pull compares the two charges. One rate table, joined at
evaluation and never snapshotted onto usage:

- **SELL** — `Model Pricing`, one **Enabled** doc per Model with one rate per counter. A pricing is
  never enabled directly: it is **Scheduled** with an `Activates At` at least **Pricing Lead**
  (Grove Settings, minutes) ahead, so the push carries it to every gateway as `next_pricing` before
  it fires. Each gateway switches to it on its own clock at `Activates At` and tags every request's
  counters with the pricing id it charged; `enable_due` then marks it Enabled and retires the
  predecessor. Editable until the lead before it fires; Disabled cancels it. Never disabled by
  hand, never re-enabled, never edited once enabled: a wrong price is a new pricing plus a credit
  row. `enabled_on` is stamped on enable and prices only records from before gateways tagged
  requests.
- **COST** — `Model Provider.rate_card` (`Model Price Row`): dated rows per vendor model id.
  Reference data only; nothing bills from it.

A counter with no sell row bills 0, and nothing is logged: the Model form shows a banner while the
model has no Enabled pricing. Every counter a model emits needs a row (vLLM emits `input_tokens`,
`cached_tokens` with `--enable-prompt-tokens-details`, `completion_tokens`; ASR emits
`audio_seconds`).

| counter | rate unit | from the response |
|---|---|---|
| `input_tokens` | USD / Mtok | prompt − cached − cache writes (guard: cached + writes > prompt → all plain) |
| `cached_tokens` | USD / Mtok | Anthropic `cache_read_input_tokens`, OpenAI/vLLM `prompt_tokens_details.cached_tokens` |
| `cache_write_tokens` | USD / Mtok | 5-minute writes: `cache_creation_input_tokens` − 1h |
| `cache_write_1h_tokens` | USD / Mtok | Anthropic `cache_creation.ephemeral_1h_input_tokens` |
| `completion_tokens` | USD / Mtok | `completion_tokens` / `output_tokens` |
| `audio_seconds` | USD / minute | `usage: {"type":"duration","seconds":N}`, else top-level `duration` |
| `request_count` | USD / request | every request |

The divisors live in `pricing.COUNTERS` and pathway's `internal/domain/price.go`; the two tables
must agree. Adding a quantity is one parser on the gateway plus one line in each.

**Revenue and usage reads.** The `Revenue` report (Desk) sums what each billed drain was charged —
Grove's cost in each record's per-model detail, records of Free users left out — grouped by model, API key, user or day over a date range,
with a total row; export from the report toolbar. Revenue only: margin against the cost card is not
built. `api.usage(users, from_date, to_date | period | month)` gives a control client tokens and cost (what was charged: usage while Free adds tokens, no cost)
per user and model (periods: Today, Yesterday, Last 7 Days, Last 30 Days, This Month, Last Month),
with `as_of` = when the newest usage in the range was pulled. Both are one grouped SQL statement
over `JSON_TABLE` of the records (`usage_record.usage_table`, built from `pricing.COUNTERS`), on
the (user, day) and (api_key, day) indexes: nothing is summed in Python.

**Nothing here is deleted.** No role holds `delete` on Grove User, Grove API Key, Grove Credit,
Usage Record, Stuck Usage, Credit Discrepancy, Model Pricing, Model or Model Provider
(`tests/test_delete_permissions.py` pins the list). A key is revoked, a pricing is superseded, a
credit is corrected by another entry. DocPerm does not bind Administrator or `ignore_permissions`;
Grove Credit's `on_trash` refuses those too. Stuck Usage is the one exception: a resolved row is
history, and Log Settings clears it after 90 days.

**The balance.** Every `Grove User` is prepaid unless marked **Free**. Top-ups are `Grove Credit`
entries — an append-only ledger, one doc per top-up or negative correction (with a note), never
edited or deleted; a control client calls `api.add_credit(email, amount, note)` or posts one
through `/api/resource/Grove Credit`, and reads `api.balance(email)` — balance, allocated, spent,
free, credit_exhausted — to show the person what they have left (`api.pull_usage(email)` first
pulls just that user's keys from every store, for a figure less than an hour old). On the user, `spent` is the USD Grove has billed and `balance` =
Σ ledger − `spent`; both are read-only and both are written by `pricing.settle`, the one writer,
which also decides `credit_exhausted = not free and balance <= 0` in both directions. It runs after
every pull, every ledger entry, every save of the form and every correction, so a top-up unblocks
the moment it is posted. A **Free** user is never charged and never gated: their usage lands as Usage Records with what it
would have cost, marked not `billed`, and `spent` and `balance` do not move on Grove or on the
gateway. Turning Free off later starts them at what they load. A
negative balance stays on the user until a top-up covers it.

**Two balances, one input.** The push carries each user `prepaid` (= not free; absent on an old
push reads as no gate), `limited` (= `credit_exhausted`) and `budget` = Σ Grove Credit in nano-USD
— the amount loaded, the same on every store. The gateway keeps its own never-reset `spent` per
user and store and refuses at `spent >= budget` (402, like `limited`). So:

- gateway balance = budget − its `spent`, priced by the gateway;
- Grove balance = Σ credits − Grove's `spent`, priced by Grove at the same pricing id.

Neither side writes the other's spend. Every user is pinned to exactly one geography (the default
one when none is picked), so all their spend lands on one store and its gateway gates them exactly
at zero; every other geography answers 403. A user on a flushed Redis is gated by Grove's `limited`
after the next pull and push. Spending one balance across geographies needs per-store slices of it
— the research and options are in the "Multi-region prepaid balance: research" doc. Nothing detects a flushed Redis or a drain lost inside the gateway: the per-drain
compare below cannot see either.

**The drain.** A pull GETs `/usage`: the gateway sets every live `usage:<prefix>` aside under a new
drain id and returns it with every earlier pair Grove has not acknowledged, grouped by drain id
(`?keys=` narrows both to one user's keys, with no scan). Grove inserts one Usage Record per key
(unique on drain id + key), bills it, commits, then POSTs `/usage/ack` with the (drain id, key)
pairs of every user that landed. Every other pair comes back next pull; a re-sent pair records
nothing twice. The acked keys are kept for Grove Settings' **Usage Retention** (rendered into each
gateway's `config.json`, default `168h`). A record is one row: the key, the store (a Link — a key
used in several geographies gets a record per store per drain), the drain id, and the totals
`cost` (what Grove billed) and `gateway_cost`; the per-model detail — pricing, requests, the
counters, Grove's cost — is one hidden JSON field the form renders as a table. The gateway's own view
of the user (its lifetime `spent` counter and the balance it gated on) goes to **Gateway Spend**,
one row per user and store, overwritten each drain.

**When the store goes down.** New requests fail closed (503). A request already running when it
went down still finishes, and its usage goes to a local spool file on the gateway (`usage_spool`)
instead of being lost; the gateway replays it into the store once the store answers, each line
once (a request-id marker). A line that still fails with the store healthy becomes a **dead line**
and rides the next pull (`dead` in the `/usage` answer): Grove lands it like any usage under the
drain id `dead:<request id>`, or keeps it as a Stuck Usage row when it cannot be read or names a key
Grove does not hold, and either way acks it so the gateway drops it. The pull's Pathway Sync row
shows `spool:N dead:N` while a gateway still holds any.

**What a pull audits** (`Credit Discrepancy`, one row per Usage Record and pricing): the gateway's
`p:<pricing>:cost` against Grove's price of the same counters at that pricing, beyond one nano-USD
per counter per request (the gateway truncates per counter). A price change never raises one: both
sides price each request at the pricing the gateway tagged it with. Nothing is corrected on its own.
A System Manager decides which side is wrong:

| button | does |
|---|---|
| Grove is wrong | Grove's `spent` moves by the delta and the user settles; Grove's balance now matches the gateway's |
| Gateway is wrong | `POST /spend-adjust` through the store's writers in turn moves the gateway's `spent` for that user on that store by minus the delta, applied once under the row's name; a failure leaves the row open and pressing again is safe |

A refund or a charge the customer is owed is a plain Grove Credit entry, separate from both. A drain
from a gateway that predates tagging has no drain id: it is priced by the day and audited for
nothing. Translations and realtime sessions carry no usage: `request_count` only.

## Scheduled jobs (`hooks.py`)

| When | Job | Note |
|---|---|---|
| `*/1` | `model_pricing.enable_due` | a Scheduled pricing whose `Activates At` has passed goes Enabled and retires its predecessor; the gateways already switched on their own clocks |
| `*/1` | `pathway.projection.sync_projection` | hash-gated: pushes each box only what it does not already hold; a fleet in sync logs nothing |
| `*/2` | `cloud_provider.reconcile.sync_all` | the provider owns whether a pod is up; this closes the drift |
| `*/5` | `gateway_store.backup_all` | one `redis-cli --rdb` snapshot per Active store, over SSH, into the weights bucket under `gateway-store/<store>/<utc stamp>.rdb`, the doc's Last Backup Key pointing at it; usage leaves a store only on the hourly pull, so this is what a lost disk costs: 5 minutes. Off until the bucket and Mirror keys are set; prune with a bucket lifecycle rule |
| hourly | `pathway.usage.pull_all` | a store is drained once, through its first writer that answers; recorded, billed, then what landed is acked. A failed pull loses nothing: the box re-sends every unacked pair. Also Gateway Server → **Pull Usage**, Stuck Usage → **Pull Now**, and `api.pull_usage(email)` for one user |
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
  whose share cannot be recorded (an unknown pricing id, a bug) is rolled back alone and left
  unacknowledged, so the gateway keeps it and re-sends it every pull; everyone else is acked. Grove
  logs it as one open **Stuck Usage** row per user and store — keys, first and last failure,
  attempts, last error and payload — updated each failing pull and resolved by the pull that lands
  it. **Pull Now** on the row pulls just that user. The usage itself lives only on the gateway
  until then, so a Redis flush loses it; Log Settings clears resolved rows after 90 days.
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
