<script setup>
import { List, ListCell, ListHeader, ListHeaderCell, ListRow, ListRows } from "frappe-ui/list";
import { computed } from "vue";

import { CARD, CARD_TITLE, LINK, MUTED, NUM, percent, usd, useFilteredCall } from "./lib.js";

const props = defineProps({ params: Object, version: Number });
const emit = defineEmits(["filter"]);
const { data, loading, error } = useFilteredCall("leaders", props);

const COLUMNS = ["minmax(0,1fr)", "5.5rem", "5.5rem"];
const BOARDS = [
	{
		key: "teams",
		title: "Top teams",
		column: "Team",
		labelClass: "",
		label: (row) => row.name || "(none)",
		sub: () => "",
		filter: (row) => ({ team: row.name }),
	},
	{
		key: "keys",
		title: "Top keys",
		column: "Key",
		labelClass: "",
		label: (row) => row.title || row.masked || row.name,
		sub: (row) => row.team || "",
		filter: (row) => ({ team: row.team || "", api_key: row.name }),
	},
	{
		key: "models",
		title: "Top models",
		column: "Model",
		labelClass: "font-mono text-xs",
		label: (row) => row.name || "(no model)",
		sub: () => "",
		filter: (row) => (row.model_doc ? { model: row.model_doc } : {}),
	},
];

const boards = computed(() =>
	BOARDS.map((board) => {
		const rows = data.value?.[board.key] || [];
		const total = rows.reduce((sum, row) => sum + row.cost, 0);
		return {
			...board,
			rows: rows.map((row, index) => ({
				...row,
				id: `${index}`,
				label: board.label(row),
				sub: board.sub(row),
				share: total ? (row.cost / total) * 100 : 0,
			})),
		};
	})
);
</script>

<template>
	<div class="grid gap-4 md:grid-cols-3">
		<section v-for="board in boards" :key="board.key" :class="CARD">
			<h2 :class="`${CARD_TITLE} mb-2`">{{ board.title }}</h2>
			<p v-if="error" class="text-sm text-ink-red-6">{{ error }}</p>
			<p v-else-if="loading && !data" :class="MUTED">Loading…</p>
			<p v-else-if="!board.rows.length" :class="MUTED">Nothing in this window.</p>
			<List v-else :columns="COLUMNS" :row-height="36">
				<ListHeader>
					<ListHeaderCell>{{ board.column }}</ListHeaderCell>
					<ListHeaderCell class="justify-end">Cost</ListHeaderCell>
					<ListHeaderCell class="justify-end">Share</ListHeaderCell>
				</ListHeader>
				<ListRows :items="board.rows" v-slot="{ item, value }">
					<ListRow :value="value">
						<ListCell class="min-w-0 gap-2">
							<button
								type="button"
								:class="[LINK, 'shrink', board.labelClass]"
								:title="`Filter the page to ${item.label}`"
								@click="emit('filter', board.filter(item))"
							>
								{{ item.label }}
							</button>
							<span v-if="item.sub" :class="`${MUTED} shrink-0 truncate`">
								{{ item.sub }}
							</span>
						</ListCell>
						<ListCell :class="NUM">{{ usd(item.cost) }}</ListCell>
						<ListCell class="justify-end gap-2">
							<span
								role="progressbar"
								:aria-valuenow="Math.round(item.share)"
								aria-valuemin="0"
								aria-valuemax="100"
								class="h-1 w-10 overflow-hidden rounded-full bg-surface-gray-3"
							>
								<span
									class="block h-full rounded-full bg-surface-gray-7"
									:style="{ width: `${item.share}%` }"
								/>
							</span>
							<span class="w-8 text-right text-xs tabular-nums text-ink-gray-5">
								{{ percent(item.share) }}
							</span>
						</ListCell>
					</ListRow>
				</ListRows>
			</List>
		</section>
	</div>
</template>
