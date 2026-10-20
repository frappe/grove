# `billing/` — what a call cost

Usage is counted under Usage Counters, priced by the Model Pricing the gateway charged, landed as Usage
Records by the drain (`../pathway/usage.py`, `reconcile.py` next to it). Counting never reads
pricing; no spend is stored, every money figure is read off the records. The pushes
and the drain are in [`../README.md`](../README.md).

## Files

| File | Owns |
|---|---|
| `pricing.py` | The counter table (`CounterTable`: the Usage Counter rows, their units, parts and bases, validated before a push), prices per counter (`PriceBook`: by the pricing id the gateway charged), `month_spent_by` (this month's spend of a team or a key, off the records and the corrections; never stored). |
| `report/revenue/` | Revenue report: what each drain was billed, cut by model, API key, team or day. |
| `report/vendor_reconciliation/` | Vendor Reconciliation report: per UTC day and model, what Grove charged users beside what the vendor billed Grove. |
| `vendor.py` | What a vendor billed Grove, read live off its billing API (`vendor_figures`): Baseten's usage and cost APIs, paged, at most 90 days. |

## Doctypes

| Doctype | Owns |
|---|---|
| `Usage Counter` | One counter usage is counted and priced under, named by its key. A root row has a unit (Mtok or request) and may be a part of a container root; a derived row is a root plus `min_prompt_tokens`, counted instead of its base when the whole prompt exceeds it and billed at the base's rate when a pricing holds none. Never edited after insert; never deleted while a Usage Record names it. Shipped in the catalog; pushed inside every pricing. Grove Control reads. |
| `Model Pricing` | A model's SELL price: one Enabled doc per model, one rate per counter. Enabling one disables the last; the next push carries it to every gateway, which tags each request with the id it charged. A Disabled draft is editable; once enabled it is never edited, disabled by hand or re-enabled, and its form is read only. Duplicate starts a new Disabled draft with its own window. A counter with no row bills 0, except a derived counter, which then bills at its base counter's row; a derived row needs that base row. A model cannot be published without one, so free on purpose is a pricing with rates 0; enabling one never publishes. The Model form shows a banner while no pricing is enabled. Carries its model's Geography, fetched, so the pricings of one key — a doc per geography — tell apart in the list. |
| `Model Pricing Rate` | One counter's sell rate (child). |
| `Usage Record` | One key's usage in one drain of one store on one UTC day — the day the gateway served it, off the gateway's clock, so a day or a month here is the gateway's own (unique on drain id + key + `day`), inserted by the pull and never updated: the key and its team, the store (Link), the drain id, the day, requests, `total_tokens` (prompt and completion once each, no part again: a cheap read beside the JSON), `cost` (Grove's price) and `gateway_cost`. Every record is audited and counts as revenue; a model served without a pricing is in it at no cost. The per-model detail — pricing, requests, counters and Grove's cost — is one hidden JSON field (`usage`) the form renders as a table, so a record is one row however many models it touched. A name Grove holds as a Model (published or not) gets an entry; the gateway's deployment-keyed metrics do not. |
| `Stuck Usage` | Usage on one store the pull keeps failing to record. For a team: the gateway holds the usage and re-sends it every pull; this row says which keys, since when, how many attempts, the last error and payload — one open row per (team, store), resolved by the pull that lands it, which lists the Usage Records it wrote on the row; **Pull Now** pulls just that team. For a dead line (`dead_line` set, team blank when unreadable): the gateway has dropped it and this row is the only copy; **Mark Resolved** closes it. An open row is never deleted; Log Settings clears resolved ones, and their list, after 90 days. |
| `Stuck Usage Record` | One Usage Record the pull that resolved a Stuck Usage row wrote (child): the record, its key and its day. The row owns the list, so a purged row takes it along and a record never points at a row that is gone. |
| `Credit Discrepancy` | One Usage Record and pricing where the gateway's cost differs from Grove's price of the same counters. Logged by the pull, never acted on by it: **Grove is wrong** lands a `Usage Correction` of the delta; **Gateway is wrong** writes a Pending `Gateway Spend Adjustment`, and the projection tick moves the gateway's `spent` for that key on that store by minus the delta (`/spend-adjust` through the store's writers, once per discrepancy). Either way the row keeps the decision alone: `resolution` and `resolved_on` (site time); blank is open. The money is a record of its own, a `Usage Correction` for Grove's side and a `Gateway Spend Adjustment` for the gateway's, and the Usage Record is never changed. System Manager only. |
| `Usage Correction` | The money of a Credit Discrepancy resolved as Grove's error: the team, the key, the pricing and its model key, the UTC day it was resolved, and the USD the key's charge moved by. One per decision (unique on the Credit Discrepancy), written by **Grove is wrong** and never changed; every money read (`usage_record.money_rows`, `pricing.month_spent_by`) sums it beside the Usage Records. System Manager and Grove Control read; nobody edits or deletes. |
| `Gateway Spend Adjustment` | The gateway side of a Credit Discrepancy resolved as the gateway's error: the store, the key, and the USD its spend counter for the key is owed (minus the delta). Pending until a box answers the projection tick's `/spend-adjust`, then Applied with the counter the box reported and when. One per decision (unique on the Credit Discrepancy), written by **Gateway is wrong**, moved to Applied by the tick, never edited by hand. System Manager reads; nobody edits or deletes. |

## Prices and caps

Money is decided in the control plane. The gateway holds its own copy — rates per pricing on its
routes, each key's budget on its record, its own spend counter per key — and gates on it. Grove
keeps its own spend per key, and every pull compares the two charges. One rate table, joined at
evaluation and never snapshotted onto usage:

- **SELL** — `Model Pricing`, one **Enabled** doc per Model with one rate per counter. Enabling one
  retires the predecessor in the same save, and the next push carries it to the gateways. Each
  gateway switches when its push lands and tags every request's counters with the pricing id it
  charged, so the pull bills the same rates whichever side of the switch a request fell on. A
  Disabled draft is editable. Never disabled by hand, never re-enabled, never edited once enabled:
  a wrong price is a new pricing plus a credit in Central. `enabled_on` is the day it was enabled.

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
| `request_count` | USD / request | every request the upstream took (2xx or a realtime 101), finished or cut short; a refusal, a 5xx or no answer is not a request |
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

**Revenue and usage reads.** The `Revenue` report (Desk) sums what each drain was charged —
Grove's cost in each record's per-model detail — plus the Usage Corrections Grove made, grouped by model, API key, team or day over a date range,
with a total row; export from the report toolbar. The `Vendor Reconciliation` report (Desk, prepared: it runs in the background and the latest run is shown) takes a vendor (a select of those with a billing read: Baseten) and a UTC day range that ends yesterday at the latest (today's figures still move on both sides) and shows, per model or per day and model (Group By), Grove's requests, input, cached and output tokens and charged cost beside the vendor's own requests, tokens and billed subtotal, with the margin and a total row; a vendor cell that disagrees with Grove's reads red. The vendor side is read live from the vendor's billing API by `vendor.py`, once per vendor account across the vendor's Model Provider records — a key row's **Account**, or the key itself when it names none, so two keys of one account (a rotation) read once and two accounts in one record both read (Baseten only so far, at most 90 days a range, the retention here); the Grove side is one grouped query over the records' per-model detail, corrections left out. A model only the vendor saw gets a row under its upstream id. `api.usage.usage(teams, from_date, to_date | period | month | pulled_after, pulled_until, key_hash)` (read-only: on the replica when the site sets `read_from_replica`) gives a control client requests and cost (what was charged; a Grove correction adds its cost under its model on the day it was resolved)
per team and model (periods: Today, Yesterday, Last 7 Days, Last 30 Days, This Month, Last Month),
and the per-model summary again per UTC day (`daily_summary`, for a chart; a day with no usage has no entry),
with `as_of` = when the newest usage in the range was pulled. `pulled_after`/`pulled_until` (RFC 3339 UTC, until exclusive) read by when a record or a correction landed (`creation`) instead of by day: Central's invoice is every record landed since its last one, so a drain that lands late goes on the next invoice and nothing is billed twice. Each is one grouped SQL statement
over `usage_record.money_rows`: `JSON_TABLE` of the records (one column per Usage Counter) in a UNION ALL with the Usage Corrections, on
the (team, day) and (api_key, day) indexes of both tables: nothing is summed in Python.

**Nothing here is deleted.** No role holds `delete` on Central Team, Grove API Key, Usage Record, Stuck Usage, Credit Discrepancy, Model Pricing, Model or Model Provider
(`tests/test_delete_permissions.py` pins the list). A key is revoked and a pricing is superseded.
DocPerm does not bind Administrator or `ignore_permissions`. Two exceptions, both through Log Settings and never by hand: a resolved Stuck Usage row is
history and is cleared after 90 days; a Usage Record served more than 90 days ago (its UTC `day`, `usage_record.RETENTION_DAYS`) is purged in chunks, except one a Credit Discrepancy names, which stays as that correction's evidence.
No money read needs a record that old (`month_spent` is this month's, an invoice is a landing window), so what goes is history: the usage API and the Revenue report stop at the retention.

**Postpaid, under the tier.** AI is billed in arrears by Central. Grove meters, prices and gates;
it holds no money that must not run out. What gates is:

- **Each key's monthly `cap`.** The gateway holds `budget` = `cap` in nano-USD on the key's
  record, counts the key's spend per UTC month on the same record (`spent:<YYYY-MM>`; a new month
  is a field not yet written, which reads 0, so nothing resets and nothing runs at midnight), and
  refuses at `spent >= budget` (402). Every key is pinned to one geography, so all its spend lands
  on one store and that store gates it exactly. A key is minted at 0 and refused until
  `api.keys.update_key(cap=)` gives it one.
- **The team's `Tier`**, named by Central from the team's trust tier (`api.teams.update_team`, by
  Central's Webhook at `api.teams.team_webhook`, and `api.teams.rename_tier` when a label changes; `Beginner`
  … `Elite`, the catalog seeds a starter ladder once and the operator edits the values in Desk):
  its `spend_limit` (USD a month), `max_keys` (0: no limit) and the rate limits a key with no
  limit rows of its own runs under. What the month has spoken for stays within the spend limit:
  each live key's cap or what it spent this month, whichever is more, plus what revoked keys spent
  (`pricing.committed`), so the keys together never outspend the tier on any mix of stores, and
  moving a spent cap to another key or revoking and minting afresh frees nothing. Only a raise, a
  mint or a revive is checked, a key's month being the records or what its box counts, whichever
  is more (`usage.peek_team`: each key's own counter, the one its cap is gated on, read in one call
  per store with no lock; refused rather than stale while a store cannot be read or pushes are
  paused). A lowered limit stops no key, it
  refuses a new cap or a raise until the caps fit; a revoke frees the key's cap, never its spend.
  A team with no tier runs under the default tier (Grove Settings), and a tier Central names
  before Grove has one is created as a copy of it. **Override Max Keys** on the team pins its own
  key count instead (`access.team_limits` resolves which apply).
  Central also sets **`suspended`**.
- **`suspended`**, set by Central when an invoice goes unpaid, pushed as `limited` on every key
  of the team (402 whatever the cap says).

**What Central reads.** `api.usage.figures(team)`: `spend` (`month_spent`, this UTC month's
records of every key the team held, revoked ones too, plus what Grove corrected, nothing
stored; `capped`, Σ live caps; `committed`, what the month has spoken for, the figure a raise is
checked against; `spend_limit`, `max_keys`, `suspended`), `keys` (each with its cap, `month_spent`
and effective limits) and `usage` (this month, every key), read together and kept in the site
cache until something moves them — a drain lands, a key or the team saved — and 30 minutes at most,
so every console page shows one moment; a correction or a tier edit waits those minutes out.
`api.usage.pull_usage(team)` drains the team now and answers its figures afresh (2 an hour per team,
then 429). `api.usage.usage(...)` reads any other window.
Credit, invoices and payment are Central's: it bills what landed since its last invoice from
`api.usage.usage(pulled_after=, pulled_until=)`, its Credit Wallet used first. Grove holds no money.

**Two spends, one input.** The gateway's `spent:<month>` per key and store, and Grove's records
per key, are each priced at the same pricing id; neither side writes the other's. A key on
a flushed Redis starts its month's counter at 0 and so gets exactly its cap again; a restore
(`restore.yml`) raises the counter on each live key to what Grove has billed it this month where
the snapshot has less, and nothing else detects a flushed Redis or a drain lost inside the gateway:
the per-drain compare below cannot see either.

**The drain.** The gateway counts each key's usage in `usage:<prefix>:<YYYY-MM-DD>`, one bucket per
UTC day it served the key, named off the clock that names the key's `spent:<month>`. A pull GETs
`/usage`: the gateway sets every live bucket aside under a new drain id and returns it with every
earlier one Grove has not acknowledged, as `{drain id: {key: {day: hash}}}` (`?keys=` narrows both
to one team's keys). Grove inserts one Usage Record per key per day, on the hash's day — so a day
or a month in Grove is the gateway's own, however late the pull — unique on drain id + key +
`day`. It bills the record to the key, commits, then POSTs `/usage/ack` with the (drain id, key, days) of every
team that landed. Every other one comes back next pull; a re-sent one records nothing twice. A day
of `""` (a gateway from before it bucketed by day) lands today. The acked counters are kept
for Grove Settings' **Usage Retention** (rendered into each gateway's `config.json`, default
`72h`). A record is one row: the key, the store (a Link — a key used in several geographies gets
a record per store per drain), the drain id, the day, and the totals: requests, `total_tokens`
(prompt and completion once each, `CounterTable.whole`: the cheap read, no JSON unnest), `cost`
(what Grove billed) and `gateway_cost`; the per-model detail — pricing, requests, the
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
| Grove is wrong | The row is stamped `Grove corrected` / `resolved_on`, and a `Usage Correction` of the delta lands under the pricing's model on today's UTC day, which every money read (usage API, Revenue, `month_spent`) sums from then on, so Grove's spend meets the gateway's; the Usage Record is untouched |
| Gateway is wrong | The row is stamped `Gateway corrected` / `resolved_on`, a Pending `Gateway Spend Adjustment` is written, and nothing is sent from the button. The next projection tick sends `POST /spend-adjust` through the store's writers in turn: the gateway's `spent` for that key on that store moves by minus the delta, applied once under the discrepancy's name, and the answer marks the adjustment Applied with the counter the box reported. A tick that fails leaves it Pending for the next; a decided row takes neither button |

A refund or a charge the customer is owed is a credit in Central, separate from both. A drain
from a gateway that predates tagging has no drain id: it is priced by the day and audited for
nothing. Translations and realtime sessions carry no usage: `request_count` only.
