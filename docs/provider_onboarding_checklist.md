# Provider and model onboarding checklist

What to look at before a vendor, or one more model of a vendor we already dial, is published.
Each item says what to find out and where the answer lands: in Grove, or in pathway (the gateway).

## Rules

1. **Measure, do not read.** A vendor's docs are a lead. Morph's reference says `/v1/messages`
   "mirrors Anthropic"; its usage does not, and each cache read was billed twice.
2. **Direct first, then through a gateway.** Direct shows what the vendor does; the gateway run
   shows what we record.
3. **Every shape we sell, plain and streamed.** Chat and messages are two vendors as far as bugs go.
4. **Per model, not per vendor.** A quirk, a default or a feature can belong to one model.
5. **Date every finding**, and write "not checked" where it was not. An empty cell reads as fine.
6. **pathway ships first.** A gateway refuses a push naming a vendor, field, quirk, metric or window
   it does not know.
7. **Say what a check compares.** A match proves only what its inputs allow (a price copied from
   another model matches itself).

## A. New provider

### A1. Account and keys

- [ ] A key test that proves the key: a real call. Name a model that does not exist: 401 is a dead
      key, 400/404 a live one, and it costs nothing. `/v1/models` may answer any bearer (Morph's does).
- [ ] Is there a separate admin or billing key? → `Model Provider Key.used_for`.
- [ ] Where spend is read: host, path, how far behind, how many decimals, per key or per account.
- [ ] Is the account used by anything else (another Grove site, hand probes)? Then spend will not
      reconcile. One account per site if it can be had.
- [ ] Caps on the account itself: member budgets, monthly limits, prepaid credit, RPM/TPM per key or
      org. What each answers when it runs out goes to A8.
- [ ] Rotation: a new `Model Provider Key`, the old one disabled. The secret is set once.

### A2. Surfaces and auth

- [ ] Which fronts it runs: OpenAI chat, Anthropic messages, embeddings. Base URL of each →
      `Model Provider.base_url`, `anthropic_base_url`.
- [ ] How each front reads the credential (Bearer, `x-api-key`), and any header it needs
      (`anthropic-version`, a beta) → the type's `Headers`, the record's Extra Headers.
- [ ] Other endpoints it has (Responses, a router, its own products): pathway dials chat, messages
      and embeddings only. Confirm nothing we mean to sell lives elsewhere.
- [ ] `/v1/models` lists what **Fetch Models** needs. Note ids that must never be an upstream id: a
      router (`auto`), an alias that moves (`*-latest`).

### A3. Residency, retention, isolation

- [ ] Where it processes, and whether a request can move region → one `Model Provider` record per
      Geography.
- [ ] Zero data retention: in writing, for our account? Only then tick the box.
- [ ] Is its prompt cache shared across callers of one key? Is there a partition field
      (`prompt_cache_key`, `user_id`, `cache_salt`)? → the type's `CacheKey` / `MetadataCacheKey`.
      None: write down that `isolate_caches` cannot split our teams there.
- [ ] Any other id the caller sends that the vendor scopes to the account: run or session tags, body
      or header. All our teams share one key, so prefix it with the team or drop it
      (`DroppedFields`). Caller headers pass through but for auth and `X-Grove-*`.

### A4. What it bills outside the token counts

- [ ] Tools it runs on its own side. Send every name under `denied_tools` in
      `grove/catalog/catalog.json` on both shapes, plus `web_search_options` and `mcp_servers`,
      with a question only a search or a sandbox could answer. Ran on its side:
      `server_tool_use` blocks, `annotations`, extra usage keys. Handed back as a plain
      `tool_use`: the client's to run, nothing to deny.
- [ ] Deny only what is billed outside tokens → a `Denied Tool` row and `catalog.json`.
- [ ] Per-request fees, and any model id or endpoint with one that a caller could reach.
- [ ] Fields that pick a price class or schedule: `service_tier`, `speed`, batch, priority. Dropped
      today: `service_tier` on every hop, `speed` on every vendor hop. A new one needs its own drop.

### A5. Usage accounting

The one that costs money. Same prompt cold then warm (a nonce first, so nothing before it can
match), on each shape, plain and streamed. Keep every raw `usage`.

- [ ] Is the cached count inside the prompt count or beside it? OpenAI: inside `prompt_tokens`.
      Anthropic: beside `input_tokens`. A compatible front may do either → `InputCountsCache`.
      Proof: the prompt count is the same cold and warm, and the same on both shapes.
- [ ] Cache writes: reported? priced? 5 minute and 1 hour apart?
- [ ] Reasoning tokens: inside the completion count? billed as output? on by default?
- [ ] Stream: where usage arrives (last chunk, every chunk, `message_start` then `message_delta`),
      and whether the first count is partial. `stream_options.include_usage` and
      `continuous_usage_stats`: taken, ignored or 400 → `UsagePerChunk`.
- [ ] A stream cut by the client: does the vendor bill it, and what do we meter?
- [ ] Usage keys we do not read (audio, image, tiers): list them.
- [ ] `model` in the answer when an alias was sent.
- [ ] Through a gateway: N known calls → our Usage Records, tokens and cost, against the vendor's
      own usage and cost report. Exact, or the difference explained.

### A6. Pricing

- [ ] The vendor's price source (URL, date) → rates in `catalog.json`.
- [ ] A rate for every counter the model reports usage under: prompt, cached, cache write (5m, 1h),
      completion, audio. A part with usage and no rate bills 0.
- [ ] Tiered rates by prompt size → the `*_above_<n>` counters.
- [ ] Discounts we do not pass on (standby, batch, off-peak): make sure the field cannot reach it.
- [ ] Rates checked against that model's own row, not a sibling's.

### A7. Request compatibility

Each is a row in pathway's `internal/service/vendor/README.md`, "What each upstream was seen to
do". Probe each; 400s and silent ignores both count.

- [ ] Output cap: `max_tokens` or `max_completion_tokens`, and is the output really capped.
- [ ] Roles: `developer`; a `system` entry inside `messages` on the Anthropic shape; the
      `tool_addition` / `tool_removal` system turn Claude Code 2.1 sends.
- [ ] Tools: a tool typed `custom`; a forced `tool_choice` while thinking; a past tool call sent back
      without its reasoning; a past tool call with no `type`; `defer_loading`.
- [ ] Sampling while reasoning: `temperature`, `top_p`, `logprobs`.
- [ ] Reasoning controls: `reasoning_effort` values, `thinking` budgets, how they map.
- [ ] JSON mode and JSON schema.
- [ ] An unknown top-level field: ignored or refused.
- [ ] Images: base64 and remote URL. Modalities it really takes, against what we declare.
- [ ] History holding another vendor's blocks (a fallback from Claude: `server_tool_use`).
- [ ] One real session per shape through a gateway: Claude Code on messages, an OpenAI SDK tool
      loop on chat. Synthetic probes missed the `tool_addition` 400.

### A8. Failures and limits

- [ ] Status and body for each: bad key, no credit, a budget spent, rate limit, overloaded, unknown
      model, prompt too long, region blocked. Each must fall in the right class (`credential`,
      `quota`, `model_access`, `model_gone`, `region_blocked`, `rate_limit`, `transient`,
      `caller_fault`); new words → a marker in `internal/domain/route.go` or the type's `Classify`.
      A quota read as a rate limit never retires the key.
- [ ] Which 4xx hide their meaning in the body → `Peeks`.
- [ ] Headers it answers with: request id, its own timing, `Retry-After`, remaining headroom →
      `Answered`.
- [ ] How often a healthy account is refused (Morph: 2 of 8 calls `service_overloaded`). Decides
      how many keys, the key selection, and whether its models need a fallback.

## B. Where it lands

pathway, released and on the gateways first:

- [ ] `internal/service/vendor/<name>.go`: a type embedding `base`, a row in `known`. Override only
      what differs: `Headers`, the quirk methods and `Quirks`, `CacheKey`, `UsagePerChunk`,
      `InputCountsCache`, `DroppedFields`, `Answered`, `Peeks`, `Classify`.
- [ ] A test per override; a dated row per finding in the vendor README table; a section per rewrite
      in `vendor_quirks.md` (its "Adding one" has the steps).

Grove:

- [ ] The name in `Model Provider.provider_name`'s options.
- [ ] `grove/catalog/catalog.json`: the provider, its models and rates, its `denied_tools`, the model
      group; the catalog paragraph in [`grove/README.md`](../grove/README.md#the-catalog-catalog).
      Never a fixture: a migrate wipes its secret.
- [ ] On the site: a `Model Provider` record per Geography (URLs, Display Name, Extra Headers, Zero
      Data Retention, key selection), a `Model Provider Key` with a Title and Used For.

## C. One more model of a known provider

- [ ] Upstream id pinned to a dated id.
- [ ] Context window and output limit.
- [ ] Input and output modalities, measured. No text out means the OpenAI surface only.
- [ ] A5 again on this model, both shapes: cold and warm usage. Do not assume the vendor's other
      model speaks for it.
- [ ] A7 again, the short set: output cap, tools, reasoning default, one real client session.
      A quirk this model does not have: pathway reads `skip_quirks` on the route row, and Grove
      has no field that sets it yet.
- [ ] Features only this model has (a scheduler tag, tools declared mid-conversation): each is an A3
      or A7 question again.
- [ ] A6 for its rates; **Load Pricing**, read the rows, enable.
- [ ] The vendor's own rate limits for it; tier `Model Limit` rows if ours should differ.
- [ ] Its model group. Then `published`, by hand.

## D. After deploy

- [ ] Through a gateway: chat and messages, plain and streamed, cold and warm → pull usage →
      tokens and cost equal the vendor's.
- [ ] A denied tool is a 400 before the dial; a dropped field shows in `X-Grove-Changed`.
- [ ] A dead key is retired and the next one answers; a 429 moves to the fallback.
- [ ] First week: `Cost Discrepancy`, `Stuck Usage`, the gateway's `request field dropped` lines, and
      the vendor's invoice against Usage Records.
- [ ] Re-run A4, A5 and A7 when the vendor ships a release note, and on a date. What we rely on
      that its docs do not state is the first thing to recheck.

## What bit us

| What happened | Caught now by |
|---|---|
| Morph's messages front counts cache reads inside `input_tokens`; each was billed twice (found 2026-11-17) | A5, cold then warm on both shapes |
| Claude Code 2.1's `tool_addition` turn was a 400 at Morph (2026-11-09) | A7, a real client session |
| A dead Morph key went unseen: `/v1/models` answers any bearer (2026-11-16) | A1, a key test that is a real call |
| Morph's `member_budget_exceeded` 429 was read as a rate limit, the key never retired | A8, a class per refusal |
| `gpt-6-luna` carried `gpt-6-sol`'s rates (2026-09-30) | A6, the model's own row; A5 reconcile |
| OpenAI's account ran out of credit (2026-10-26) | A1 caps on the account; A8 `quota` |
| DeepSeek's Anthropic front ran web search with no listed price (2026-10-09) | A4 |
| Morph's DSV4.1 Flash declared Text only though it reads images | C, modalities measured |
| Run tags shared by every team on one Morph key | A3, ids the vendor scopes to the account |
