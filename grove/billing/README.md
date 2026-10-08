# `billing/` — what a call cost, and who has paid

Usage is counted under Usage Counters, priced by the Model Pricing the gateway charged, landed as Usage
Records by the drain (`../pathway/usage.py`, `reconcile.py` next to it) and moves a prepaid user's
`spent`. Counting never reads pricing; `pricing.settle` is the one writer of the verdict. The pushes
and the drain are in [`../README.md`](../README.md).

## Files

| File | Owns |
|---|---|
| `pricing.py` | The counter table (`CounterTable`: the Usage Counter rows, their units, parts and bases, validated before a push), prices per counter (`PriceBook`: by the pricing id the gateway charged), the prepaid balance, `settle` (the one writer of a team's `spent`, `balance` and `credit_exhausted`). |
| `report/revenue/` | Revenue report: what each drain was billed, cut by model, API key, team or day. |

## Doctypes

| Doctype | Owns |
|---|---|
| `Usage Counter` | One counter usage is counted and priced under, named by its key. A root row has a unit (Mtok or request) and may be a part of a container root; a derived row is a root plus `min_prompt_tokens`, counted instead of its base when the whole prompt exceeds it and billed at the base's rate when a pricing holds none. Never edited after insert; never deleted while a Usage Record names it. Shipped in the catalog; pushed inside every pricing. Grove Control reads. |
| `Model Pricing` | A model's SELL price: one Enabled doc per model, one rate per counter. Enabling one disables the last; the next push carries it to every gateway, which tags each request with the id it charged. A Disabled draft is editable; once enabled it is never edited, disabled by hand or re-enabled, and its form is read only. Duplicate starts a new Disabled draft with its own window. A counter with no row bills 0, except a derived counter, which then bills at its base counter's row; a derived row needs that base row. A model cannot be published without one, so free on purpose is a pricing with rates 0; enabling one never publishes. The Model form shows a banner while no pricing is enabled. Carries its model's Geography, fetched, so the pricings of one key — a doc per geography — tell apart in the list. |
| `Model Pricing Rate` | One counter's sell rate (child). |
| `Grove Credit` | One ledger entry on a team: a top-up, or a negative correction with a note. Append-only — never edited or deleted, a wrong entry is corrected by another; a control client posts one through `/api/resource`. `reference` is the caller's own id for the top-up, unique across the ledger, so a repeated `api.add_credit` adds nothing. Its `on_update` settles the team. |
| `Usage Record` | One key's usage in one drain of one store, billed and free apart (unique on drain id + key + `billed`), inserted by the pull and never updated: the key and its team, the store (Link), the drain id, requests, `cost` (Grove's price), `gateway_cost`, and `billed` — whether it was charged, which is whether the gateway served it while the team was prepaid (it tags each request `p:` or `f:`, so a drain that spans a flip lands as two records). Only billed records move the key's `spent`, are audited and count as revenue. The per-model detail — pricing, requests, counters and Grove's cost — is one hidden JSON field (`usage`) the form renders as a table, so a record is one row however many models it touched. A name Grove holds as a Model (published or not) gets an entry; the gateway's deployment-keyed metrics do not. |
| `Stuck Usage` | Usage on one store the pull keeps failing to record. For a team: the gateway holds the usage and re-sends it every pull; this row says which keys, since when, how many attempts, the last error and payload — one open row per (team, store), resolved by the pull that lands it; **Pull Now** pulls just that team. For a dead line (`dead_line` set, team blank when unreadable): the gateway has dropped it and this row is the only copy; **Mark Resolved** closes it. An open row is never deleted; Log Settings clears resolved ones after 90 days. |
| `Credit Discrepancy` | One Usage Record and pricing where the gateway's cost differs from Grove's price of the same counters. Logged by the pull, never acted on by it: **Grove is wrong** moves the key's `spent` by the delta and settles the team; **Gateway is wrong** ticks the row's `Gateway Correction Pending`, and the projection tick moves the gateway's `spent` for that key on that store by minus the delta (`/spend-adjust` through the store's writers, once per row). Either way the row keeps the correction and its `resolution`; blank and not pending is open. System Manager only. |

## Prices and credits

Money is decided in the control plane. The gateway holds its own copy — rates per pricing on its
routes, each key's cap on its record, its own spend counter per key — and gates on it. Grove
keeps its own balance, and every pull compares the two charges. One rate table, joined at
evaluation and never snapshotted onto usage:

- **SELL** — `Model Pricing`, one **Enabled** doc per Model with one rate per counter. Enabling one
  retires the predecessor in the same save, and the next push carries it to the gateways. Each
  gateway switches when its push lands and tags every request's counters with the pricing id it
  charged, so the pull bills the same rates whichever side of the switch a request fell on. A
  Disabled draft is editable. Never disabled by hand, never re-enabled, never edited once enabled:
  a wrong price is a new pricing plus a credit row. `enabled_on` is the day it was enabled.

**No pricing, no serving.** A model is routed only while it is **Published**
(`routes.published_routes`), and Published is ticked by hand: the save refuses a model with no
Enabled pricing or with nothing serving it. Enabling a pricing never publishes, nor does a replica
going Active; a model nothing serves any more is unpublished on its own and re-ticked by hand.
Free on purpose is a pricing with rates 0 — a self-hosted ASR, an internal model — so a missing
pricing always means a model not yet on sale. The Model form shows a banner while it has none, and
another once Published while no grant names it (`Model.is_granted`: a user's Allow, or a Model Group
with a user in it) — routed, but every key is told access not allowed and `/v1/models` omits it.

Usage is recorded whatever the price. A counter with no sell row bills 0, and nothing is logged.
Every counter a model emits needs a row (vLLM emits `prompt_tokens`,
`cached_tokens` with `--enable-prompt-tokens-details`, `completion_tokens`). A transcription that
reports a duration moves no priced counter but `request_count`: audio is priced by the token.

The counters are the `Usage Counter` table, shipped in the catalog (`catalog/catalog.json`,
inserted when missing) and pushed to the gateways inside each pricing, so the two sides count
under the same rows. A **root** counter is a bucket the gateway fills from the response; a
**derived** one is a root plus a condition, counted instead of its base when the request's whole
prompt exceeds `min_prompt_tokens`. Today's rows:

| counter | rate unit | from the response |
|---|---|---|
| `prompt_tokens` | USD / Mtok | the whole prompt; its rate charges what the four counters below left of it (guard: cached + writes > prompt → all plain) |
| `cached_tokens` | USD / Mtok | Anthropic `cache_read_input_tokens`, OpenAI/vLLM `prompt_tokens_details.cached_tokens` |
| `cache_write_tokens` | USD / Mtok | Anthropic 5-minute writes: `cache_creation_input_tokens` − 1h; OpenAI `prompt_tokens_details.cache_write_tokens` |
| `cache_write_1h_tokens` | USD / Mtok | Anthropic `cache_creation.ephemeral_1h_input_tokens` |
| `audio_tokens` | USD / Mtok | audio in the prompt: `prompt_tokens_details.audio_tokens`, a transcription's `input_token_details.audio_tokens`; capped at what the cache left |
| `completion_tokens` | USD / Mtok | the whole completion, `completion_tokens` / `output_tokens`; its rate charges what audio output left of it |
| `completion_audio_tokens` | USD / Mtok | audio in the completion: `completion_tokens_details.audio_tokens`; capped at the completion |
| `request_count` | USD / request | every request |
| `*_above_272k` | USD / Mtok | derived from `prompt_tokens`, `cached_tokens`, `cache_write_tokens` and `completion_tokens` at 272 000: that counter's tokens of a request whose prompt exceeds it |

**The prompt rate charges the uncached part.** Counting records what the vendor reports; charging
subtracts (`pricing.cost`, pathway `domain.Cost`). A counter's rate is charged on what its parts
left of it, and each part at its own rate, so a token bills once:

`part_of` on a root row names its container (`cached_tokens`, `cache_write_tokens`,
`cache_write_1h_tokens` and `audio_tokens` are parts of `prompt_tokens`; `completion_audio_tokens`
of `completion_tokens`). A derived counter's parts are its base's parts at the same threshold.

A part with no row bills 0, like any counter: a pricing with no `cached_tokens` row gives cached
tokens away, and an audio-out model needs a `completion_audio_tokens` row.

**Above 272k.** A vendor that charges a higher rate for the whole request once the prompt passes
272 000 tokens (OpenAI) is priced with the `_above_272k` rows. Two rules:

- **Counting**, in pathway, from the pushed table. A request whose prompt (plain, cached, written
  and audio together) is strictly past a derived counter's threshold is counted under that counter
  instead of its base, whatever the pricing. A root with no derived row at that threshold stays
  where it is: hour writes and audio have one rate at any prompt size, so a long request's are
  counted in `prompt_tokens`, its audio output in `completion_tokens`, and only the rest above
  272k. That keeps a summed drain priced as its requests would be apart.
- **Charging**, the same on both sides (`pricing.cost`, pathway `domain.CounterTable.Cost`). A
  counter bills at its own rate. A derived counter the pricing holds no row for bills at its base's
  rate, so a pricing without those rows charges as it always did.

A derived row needs its base row (`Model Pricing` refuses it otherwise): a shorter prompt bills
there. A part is bracketed exactly like its container or not at all (`CounterTable.validate`,
run before every push): otherwise a request could land a part's variant inside a container
variant whose parts do not subtract it.

A new bracket or a new vendor fee is rows, not a release: a derived counter is a doc; a new root
is a doc plus the one parser line in pathway that fills its bucket from the response.

**Revenue and usage reads.** The `Revenue` report (Desk) sums what each billed drain was charged —
Grove's cost in each record's per-model detail, records of Free teams left out — grouped by model, API key, team or day over a date range,
with a total row; export from the report toolbar. Revenue only, no margin. `api.usage(teams, from_date, to_date | period | month, key_hash)` (read-only: on the replica when the site sets `read_from_replica`) gives a control client requests and cost (what was charged: usage while Free adds requests, no cost)
per team and model (periods: Today, Yesterday, Last 7 Days, Last 30 Days, This Month, Last Month),
and the per-model summary again per UTC day (`daily_summary`, for a chart; a day with no usage has no entry),
with `as_of` = when the newest usage in the range was pulled. Each is one grouped SQL statement
over `JSON_TABLE` of the records (`usage_record.usage_table`, one column per Usage Counter), on
the (team, day) and (api_key, day) indexes: nothing is summed in Python.

**Nothing here is deleted.** No role holds `delete` on Central Team, Grove API Key, Grove Credit,
Usage Record, Stuck Usage, Credit Discrepancy, Model Pricing, Model or Model Provider
(`tests/test_delete_permissions.py` pins the list). A key is revoked, a pricing is superseded, a
credit is corrected by another entry. DocPerm does not bind Administrator or `ignore_permissions`;
Grove Credit's `on_trash` refuses those too. Stuck Usage is the one exception: a resolved row is
history, and Log Settings clears it after 90 days.

**The balance.** Every `Central Team` is prepaid unless marked **Free**. Top-ups are `Grove Credit`
entries — an append-only ledger, one doc per top-up or negative correction (with a note), never
edited or deleted; a control client calls `api.add_credit(team, amount, note, reference)` or posts one
through `/api/resource/Grove Credit` (`reference` is the client's own id for the top-up, unique on
the ledger: `add_credit` repeated with one adds nothing, so a call that timed out is sent again
safely, and the same id on another team or amount is refused), and reads `api.balance(team)` — balance, spent,
unallocated, is_free_user — to show the team what it has left (`api.pull_usage(team)` first
pulls just that team's keys from every store, for a figure less than an hour old; 2 an hour per
team, then 429). On the team, `spent` is Σ what its keys were billed and `balance` =
Σ ledger − `spent`; both are read-only and both are written by `pricing.settle`, the one writer,
which also decides `credit_exhausted = not free and balance <= 0` in both directions. It runs after
every pull, every ledger entry, every save of the form and every correction, so a top-up unblocks
the moment it is posted. A **Free** team is never charged and never gated: its usage lands as Usage Records with what it
would have cost, marked not `billed`, and `spent` and `balance` do not move on Grove or on the
gateway. Turning Free off later starts it at what it loads. The gateway decides per request:
Free takes effect when the push reaches it (the next sync), and the pull bills what it tagged
`p:`, never the team's flag at the time of the pull. A
negative balance stays on the team until a top-up covers it.

**Caps: the balance handed out per key.** A team spends only through its keys, and each key may
spend only its `cap` — the slice of the balance cut for it (OpenRouter's per-key credit limit, with
the balance pooled on the team). Grove keeps Σ (cap − spent) over the team's live keys within the
balance: a cap that would hand out more is refused, a revoke hands the key's share back, and
`api.balance(team).unallocated` is what no cap has claimed yet. A prepaid team's key needs a cap
above zero (`api.provision_key(cap=)`, `api.update_key(cap=)`): minting one with none is refused.
The other two writes that could take the balance under the caps are guarded too: a negative Grove
Credit that would leave less than the caps hand out is refused (lower the caps first), and turning
Free off resets every live key's cap to 0, since a Free team's caps are never checked. What is
left is a box overshooting a cap by what was in flight, bounded by concurrent requests × their
size; the team's `limited` catches it at the next pull.

**Two balances, one input.** The push carries each key `prepaid` (= its team is not free; absent
on an old push reads as no gate), `limited` (= the team's `credit_exhausted`) and `budget` = its cap
in nano-USD. The gateway keeps its own never-reset `spent` per key and store and refuses at
`spent >= budget` (402, like `limited`). So:

- gateway balance for a key = cap − its `spent`, priced by the gateway;
- Grove balance for the team = Σ credits − Σ its keys' `spent`, priced by Grove at the same pricing id.

Neither side writes the other's spend. Every key is pinned to exactly one geography (the default
one when none is picked), so all its spend lands on one store and its gateway gates the cap exactly
at zero; every other geography answers 403. A team spans geographies by minting a key in each, each
with its own cap, and the caps together never exceed the balance — no slice of one balance is ever
shared between stores. A key on a flushed Redis is gated by Grove's `limited` after the next pull
and push; so is every key of a team whose balance a refund took under its caps. Nothing detects a
flushed Redis or a drain lost inside the gateway: the per-drain compare below cannot see either.

**The drain.** A pull GETs `/usage`: the gateway sets every live `usage:<prefix>` aside under a new
drain id and returns it with every earlier pair Grove has not acknowledged, grouped by drain id
(`?keys=` narrows both to one team's keys, with no scan). Grove inserts one Usage Record per key
— two when the drain holds both charged (`p:`) and free (`f:`) usage of it; unique on drain id +
key + `billed` — bills the charged one to the key, commits, then POSTs `/usage/ack` with the (drain
id, key) pairs of every team that landed. Every other pair comes back next pull; a re-sent pair records
nothing twice. The acked keys are kept for Grove Settings' **Usage Retention** (rendered into each
gateway's `config.json`, default `168h`). A record is one row: the key, the store (a Link — a key
used in several geographies gets a record per store per drain), the drain id, and the totals
`cost` (what Grove billed) and `gateway_cost`; the per-model detail — pricing, requests, the
counters, Grove's cost — is one hidden JSON field the form renders as a table. The gateway's own view
of the key (`key_spent`, `key_balance`) rides the same hash and is not recorded.

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
| Grove is wrong | The key's `spent` moves by the delta and the team settles; Grove's balance now matches the gateway's |
| Gateway is wrong | The row's `Gateway Correction Pending` is ticked and nothing is sent from the button. The next projection tick sends `POST /spend-adjust` through the store's writers in turn: the gateway's `spent` for that key on that store moves by minus the delta, applied once under the row's name, and the answer marks the row `Gateway corrected` and clears the tick. A tick that fails leaves it pending for the next; a pending row takes neither button |

A refund or a charge the customer is owed is a plain Grove Credit entry, separate from both. A drain
from a gateway that predates tagging has no drain id: it is priced by the day and audited for
nothing. Translations and realtime sessions carry no usage: `request_count` only.
