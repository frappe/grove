<script setup>
// The chosen team's keys (every one of them), else the window's costliest: each against its cap,
// with its limits and when it was last served. Searched and paged on the client; the read is
// at most KEY_LIMIT rows.
import { useHost } from "@framework/ui/island";
import { Badge, Button, dayjs } from "frappe-ui";
import { List, ListCell, ListHeader, ListHeaderCell, ListRow, ListRows } from "frappe-ui/list";
import { computed, ref, watch } from "vue";

import {
	CARD,
	CARD_TITLE,
	CELL,
	LINK,
	MUTED,
	NUM,
	compact,
	count,
	day,
	percent,
	usd,
	useCall,
} from "./lib.js";

const props = defineProps({ params: Object, version: Number });
const emit = defineEmits(["filter"]);
const host = useHost();
const { data, loading, error, load } = useCall("keys");

watch(
	() => [props.params, props.version],
	() => load({ filters: props.params }),
	{ immediate: true }
);

const CHIPS = [
	{ key: "near", label: "Near cap", test: (row) => row.cap_pct !== null && row.cap_pct >= 80 },
	{
		key: "idle",
		label: "Idle 30 d",
		test: (row) =>
			row.status === "active" &&
			(!row.last_used || dayjs().diff(dayjs(row.last_used), "day") >= 30),
	},
	{ key: "revoked", label: "Revoked", test: (row) => row.status === "revoked" },
];
const PAGE_SIZE = 10;
const chip = ref("");
const search = ref("");
const page = ref(1);

const title = computed(() => (props.params?.team ? `Keys of ${props.params.team}` : "Keys"));
const rows = computed(() => {
	const cut = CHIPS.find((candidate) => candidate.key === chip.value);
	const needle = search.value.trim().toLowerCase();
	return (data.value || [])
		.filter((row) => !cut || cut.test(row))
		.filter(
			(row) =>
				!needle ||
				[row.title, row.masked, row.team, row.name].some((text) =>
					String(text || "")
						.toLowerCase()
						.includes(needle)
				)
		)
		.map((row) => ({ ...row, id: row.name }));
});
const pageCount = computed(() => Math.max(1, Math.ceil(rows.value.length / PAGE_SIZE)));
const paged = computed(() =>
	rows.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE)
);
const range = computed(() => {
	const first = (page.value - 1) * PAGE_SIZE + 1;
	return `${first}–${Math.min(page.value * PAGE_SIZE, rows.value.length)} of ${
		rows.value.length
	}`;
});

watch([search, chip, data], () => (page.value = 1));
watch(pageCount, (total) => (page.value = Math.min(page.value, total)));

const COLUMNS = [
	"minmax(0,1.4fr)",
	"minmax(0,1fr)",
	"minmax(0,1.2fr)",
	"9rem",
	"5rem",
	"6rem",
	"6rem",
	"2.5rem",
];
const METRIC = { requests: "R", total_tokens: "T" };
const WINDOW = { "1m": "PM", "1h": "PH", "1d": "PD", "1M": "PMo" };
const chips = (limits) =>
	(limits || []).map((limit) => ({
		key: `${limit.metric}:${limit.window}`,
		label: `${compact(limit.value)} ${METRIC[limit.metric] || limit.metric}${
			WINDOW[limit.window] || ""
		}`,
		text: `${count(limit.value)} ${limit.metric} per ${limit.window}`,
	}));

const barClass = (row) =>
	row.cap_pct >= 100
		? "bg-surface-red-5"
		: row.cap_pct >= 80
		? "bg-surface-amber-3"
		: "bg-surface-gray-7";
const open = (row) => host.navigate?.(["Form", "Grove API Key", row.name]);
</script>

<template>
	<section :class="CARD">
		<header class="mb-2 flex flex-wrap items-center gap-2">
			<h2 :class="`${CARD_TITLE} mr-2`">{{ title }}</h2>
			<Button
				v-for="candidate in CHIPS"
				:key="candidate.key"
				size="sm"
				:variant="chip === candidate.key ? 'solid' : 'subtle'"
				:label="candidate.label"
				@click="chip = chip === candidate.key ? '' : candidate.key"
			/>
			<input
				v-model="search"
				type="search"
				placeholder="Search title, prefix, team"
				aria-label="Search keys"
				class="ml-auto h-7 w-60 rounded border border-outline-gray-2 bg-surface-white px-2 text-base text-ink-gray-8 placeholder:text-ink-gray-4 focus:outline-none focus:ring-2 focus:ring-outline-gray-3"
			/>
		</header>
		<p v-if="error" class="text-sm text-ink-red-6">{{ error }}</p>
		<p v-else-if="loading && !data" :class="MUTED">Loading…</p>
		<p v-else-if="!rows.length" :class="MUTED">
			No key matches.<template v-if="!params?.team">
				Pick a team to list all of its keys.</template
			>
		</p>
		<template v-else>
			<div class="overflow-x-auto">
				<List :columns="COLUMNS" :row-height="48" class="min-w-[60rem]">
					<ListHeader>
						<ListHeaderCell>Key</ListHeaderCell>
						<ListHeaderCell>Team</ListHeaderCell>
						<ListHeaderCell>Cap</ListHeaderCell>
						<ListHeaderCell>Limits</ListHeaderCell>
						<ListHeaderCell>Last used</ListHeaderCell>
						<ListHeaderCell class="justify-end">Requests</ListHeaderCell>
						<ListHeaderCell class="justify-end">Cost</ListHeaderCell>
						<ListHeaderCell />
					</ListHeader>
					<ListRows :items="paged" v-slot="{ item, value }">
						<ListRow
							:value="value"
							:class="item.status === 'revoked' && 'text-ink-gray-5'"
						>
							<ListCell class="flex-col !items-start justify-center">
								<span class="flex max-w-full items-center gap-2">
									<button
										type="button"
										:class="`${LINK} font-medium`"
										title="Filter the page to this key"
										@click="
											emit('filter', {
												team: item.team || '',
												api_key: item.name,
											})
										"
									>
										{{ item.title || item.masked }}
									</button>
									<Badge v-if="item.status === 'revoked'" label="Revoked" />
									<Badge v-else-if="!item.cap" theme="amber" label="No cap" />
								</span>
								<span class="font-mono text-xs text-ink-gray-5">{{
									item.masked
								}}</span>
							</ListCell>
							<ListCell>
								<button
									type="button"
									:class="LINK"
									:title="`Filter the page to ${item.team}`"
									@click="emit('filter', { team: item.team })"
								>
									{{ item.team }}
								</button>
							</ListCell>
							<ListCell class="flex-col !items-start justify-center gap-1">
								<span class="text-sm tabular-nums text-ink-gray-5">
									<span class="text-ink-gray-9">{{
										usd(item.month_spent)
									}}</span>
									<template v-if="item.cap"> / {{ usd(item.cap) }}</template>
									<span v-if="item.cap_pct !== null" class="ml-1">{{
										percent(item.cap_pct)
									}}</span>
								</span>
								<span
									v-if="item.cap && item.status === 'active'"
									class="block h-1 w-full overflow-hidden rounded-full bg-surface-gray-3"
								>
									<span
										class="block h-full rounded-full"
										:class="barClass(item)"
										:style="{ width: `${Math.min(100, item.cap_pct)}%` }"
									/>
								</span>
							</ListCell>
							<ListCell
								class="flex-wrap gap-x-2 text-sm tabular-nums text-ink-gray-7"
							>
								<span
									v-for="limit in chips(item.limits)"
									:key="limit.key"
									:title="limit.text"
								>
									{{ limit.label }}
								</span>
							</ListCell>
							<ListCell :class="CELL">{{ day(item.last_used) }}</ListCell>
							<ListCell :class="NUM">{{ count(item.requests) }}</ListCell>
							<ListCell :class="`${NUM} font-medium text-ink-gray-9`">{{
								usd(item.cost)
							}}</ListCell>
							<ListCell class="justify-end">
								<Button
									variant="ghost"
									size="sm"
									icon="lucide-arrow-up-right"
									:aria-label="`Open key ${item.title || item.masked}`"
									@click="open(item)"
								/>
							</ListCell>
						</ListRow>
					</ListRows>
				</List>
			</div>
			<footer class="mt-2 flex items-center justify-end gap-2">
				<span :class="MUTED">{{ range }}</span>
				<Button
					variant="subtle"
					size="sm"
					icon="lucide-chevron-left"
					aria-label="Previous page"
					:disabled="page <= 1"
					@click="page--"
				/>
				<span class="text-sm tabular-nums text-ink-gray-7"
					>{{ page }} / {{ pageCount }}</span
				>
				<Button
					variant="subtle"
					size="sm"
					icon="lucide-chevron-right"
					aria-label="Next page"
					:disabled="page >= pageCount"
					@click="page++"
				/>
			</footer>
		</template>
	</section>
</template>
