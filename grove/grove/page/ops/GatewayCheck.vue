<script setup>
// On demand, for one team: what the boxes count this month beside what Grove's records hold.
// Never on load; the boxes are dialled only when the button is pressed.
import { useHost } from "@framework/ui/island";
import { Badge, Button, Tooltip } from "frappe-ui";
import { List, ListCell, ListHeader, ListHeaderCell, ListRow, ListRows } from "frappe-ui/list";
import { computed, watch } from "vue";

import { CARD, CARD_TITLE, CELL, MUTED, NUM, signedUsd, usd, useCall } from "./lib.js";

const props = defineProps({ team: { type: String, default: "" } });
const host = useHost();
const { data, loading, error, load } = useCall("peek");
const open = (row) => host.navigate?.(["Form", "Grove API Key", row.key]);

watch(
	() => props.team,
	() => (data.value = null)
);

const COLUMNS = ["minmax(0,1fr)", "6rem", "6rem", "6rem", "minmax(0,1.6fr)", "2.5rem"];
const rows = computed(() => (data.value?.rows || []).map((row) => ({ ...row, id: row.key })));

const reading = (row) => {
	if (row.gateway == null) return { label: "not on a store", theme: "gray" };
	if (row.delta == null) return { label: "lifetime counter", theme: "gray" };
	if (Math.abs(row.delta) < 1e-9) return { label: "in step", theme: "green" };
	return row.delta < 0 ? { label: "box behind", theme: "amber" } : { label: "box ahead", theme: "blue" };
};
</script>

<template>
	<section :class="CARD">
		<header class="flex flex-wrap items-center justify-between gap-3">
			<div>
				<h2 :class="CARD_TITLE">Gateway check</h2>
				<p :class="MUTED">Grove's month per key beside the counter the boxes gate on.</p>
			</div>
			<Tooltip :text="team ? `Dial the stores for ${team}` : 'Pick a team in the filters first'">
				<Button
					variant="subtle"
					icon-left="lucide-radar"
					:label="team ? `Check ${team}` : 'Check gateway'"
					:disabled="!team"
					:loading="loading"
					@click="load({ team })"
				/>
			</Tooltip>
		</header>
		<p v-if="error" class="mt-3 text-sm text-ink-red-6">{{ error }}</p>
		<List v-else-if="rows.length" :columns="COLUMNS" :row-height="36" class="mt-3">
			<ListHeader>
				<ListHeaderCell>Key</ListHeaderCell>
				<ListHeaderCell class="justify-end">Grove</ListHeaderCell>
				<ListHeaderCell class="justify-end">Gateway</ListHeaderCell>
				<ListHeaderCell class="justify-end">Delta</ListHeaderCell>
				<ListHeaderCell>Reading</ListHeaderCell>
				<ListHeaderCell />
			</ListHeader>
			<ListRows :items="rows" v-slot="{ item, value }">
				<ListRow :value="value" :class="item.status === 'revoked' && 'text-ink-gray-5'">
					<ListCell class="gap-2">
						<span :class="`${CELL} truncate`">{{ item.title || item.key }}</span>
						<Badge v-if="item.status === 'revoked'" label="Revoked" />
					</ListCell>
					<ListCell :class="NUM">{{ usd(item.grove) }}</ListCell>
					<ListCell :class="NUM">{{ item.gateway == null ? "—" : usd(item.gateway) }}</ListCell>
					<ListCell :class="NUM">{{ item.delta == null ? "—" : signedUsd(item.delta) }}</ListCell>
					<ListCell class="gap-2">
						<Badge :theme="reading(item).theme" :label="reading(item).label" />
						<span :class="`${MUTED} truncate`" :title="item.verdict">{{ item.verdict }}</span>
					</ListCell>
					<ListCell class="justify-end">
						<Button
							variant="ghost"
							size="sm"
							icon="lucide-arrow-up-right"
							:aria-label="`Open key ${item.title || item.key}`"
							@click="open(item)"
						/>
					</ListCell>
				</ListRow>
			</ListRows>
		</List>
	</section>
</template>
