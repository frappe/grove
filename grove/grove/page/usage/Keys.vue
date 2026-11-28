<script setup>
// Searched and paged on the client; the read is at most KEY_LIMIT rows.
import { useHost } from "@framework/ui/island";
import { Badge, Button, dayjs } from "frappe-ui";
import { List, ListCell, ListHeader, ListHeaderCell, ListRow, ListRows } from "frappe-ui/list";
import { computed, ref, watch } from "vue";

import CapBar from "./CapBar.vue";
import {
	CARD,
	CARD_TITLE,
	CELL,
	LINK,
	MUTED,
	NEAR_CAP,
	NUM,
	count,
	day,
	percent,
	usd,
	useFilteredCall,
} from "./lib.js";

const props = defineProps({ params: Object, version: Number });
const emit = defineEmits(["filter"]);
const host = useHost();
const { data, loading, error } = useFilteredCall("keys", props);

const CHIPS = [
	{
		key: "near",
		label: "Near cap",
		test: (row) => row.cap_pct !== null && row.cap_pct >= NEAR_CAP,
	},
	{
		key: "idle",
		label: "Idle 30 d",
		test: (row) =>
			row.status === "active" &&
			(!row.last_used || dayjs().diff(dayjs(row.last_used), "day") >= 30),
	},
	{ key: "revoked", label: "Revoked", test: (row) => row.status === "revoked" },
	{ key: "transferred", label: "Transferred", test: (row) => row.transfers.length > 0 },
];
const PAGE_SIZE = 10;
const chip = ref("");
const search = ref("");
const page = ref(1);

const SEARCHED = ["title", "masked", "team", "name"];
const matches = (row, needle) =>
	SEARCHED.some((field) =>
		String(row[field] || "")
			.toLowerCase()
			.includes(needle)
	);

const title = computed(() => (props.params?.team ? `Keys of ${props.params.team}` : "Keys"));
const rows = computed(() => {
	const cut = CHIPS.find((candidate) => candidate.key === chip.value);
	const needle = search.value.trim().toLowerCase();
	return (data.value || [])
		.filter((row) => !cut || cut.test(row))
		.filter((row) => !needle || matches(row, needle))
		.map(describe);
});
const pageCount = computed(() => Math.max(1, Math.ceil(rows.value.length / PAGE_SIZE)));
const paged = computed(() =>
	rows.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE)
);
const range = computed(() => {
	const first = (page.value - 1) * PAGE_SIZE + 1;
	const last = Math.min(page.value * PAGE_SIZE, rows.value.length);
	return `${first}–${last} of ${rows.value.length}`;
});

watch([search, chip, data], () => {
	page.value = 1;
});
watch(pageCount, (total) => {
	page.value = Math.min(page.value, total);
});

const COLUMNS = [
	"minmax(0,1.4fr)",
	"minmax(0,1fr)",
	"minmax(0,1.2fr)",
	"5rem",
	"6rem",
	"6rem",
	"2.5rem",
];
const hops = (transfers) =>
	transfers
		.map((move) => `${move.from_team} → ${move.to_team}, ${day(move.creation)}`)
		.join("\n");

function describe(row) {
	const moved = row.transfers.at(-1);
	// Listed under a team that handed it over: the usage is that team's, the cap is not.
	const away = Boolean(props.params?.team) && row.team !== props.params.team;
	return {
		...row,
		id: row.name,
		moved,
		movedTitle: moved && `Show this key under ${moved.from_team}\n${hops(row.transfers)}`,
		away,
		showBar: !away && Boolean(row.cap) && row.status === "active",
	};
}

function toggleChip(key) {
	chip.value = chip.value === key ? "" : key;
}

function filterToKey(team, key) {
	emit("filter", { team, api_key: key });
}

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
				@click="toggleChip(candidate.key)"
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
						<ListHeaderCell>Last used</ListHeaderCell>
						<ListHeaderCell class="justify-end">Requests</ListHeaderCell>
						<ListHeaderCell class="justify-end">Cost</ListHeaderCell>
						<ListHeaderCell />
					</ListHeader>
					<ListRows :items="paged" v-slot="{ item, value }">
						<ListRow
							:value="value"
							:class="{ 'text-ink-gray-5': item.status === 'revoked' }"
						>
							<ListCell class="flex-col !items-start justify-center">
								<span class="flex max-w-full items-center gap-2">
									<button
										type="button"
										:class="`${LINK} font-medium`"
										title="Filter the page to this key"
										@click="filterToKey(item.team || '', item.name)"
									>
										{{ item.title || item.masked }}
									</button>
									<Badge
										v-if="item.away"
										theme="blue"
										label="Transferred"
										:title="`Now ${item.team}'s`"
									/>
									<Badge v-else-if="item.status === 'revoked'" label="Revoked" />
									<Badge v-else-if="!item.cap" theme="amber" label="No cap" />
								</span>
								<span class="font-mono text-xs text-ink-gray-5">{{
									item.masked
								}}</span>
							</ListCell>
							<ListCell class="flex-col !items-start justify-center">
								<button
									type="button"
									:class="LINK"
									:title="`Filter the page to ${item.team}`"
									@click="emit('filter', { team: item.team })"
								>
									{{ item.team }}
								</button>
								<button
									v-if="item.moved"
									type="button"
									:class="`${MUTED} max-w-full truncate text-left underline-offset-2 hover:underline`"
									:title="item.movedTitle"
									@click="filterToKey(item.moved.from_team, item.name)"
								>
									from {{ item.moved.from_team }} ·
									{{ day(item.moved.creation) }}
								</button>
							</ListCell>
							<ListCell class="flex-col !items-start justify-center gap-1">
								<span
									v-if="!item.away"
									class="flex w-full justify-between gap-2 text-sm tabular-nums text-ink-gray-5"
								>
									<span>
										<span class="text-ink-gray-9">{{
											usd(item.month_spent)
										}}</span>
										<template v-if="item.cap"> / {{ usd(item.cap) }}</template>
									</span>
									<span v-if="item.cap_pct !== null">{{
										percent(item.cap_pct)
									}}</span>
								</span>
								<CapBar v-if="item.showBar" :pct="item.cap_pct" />
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
