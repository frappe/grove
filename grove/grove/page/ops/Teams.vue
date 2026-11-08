<script setup>
// Every team the window saw: tier, live keys, this month against its spend limit, last served,
// and the window's usage.
import { useHost } from "@framework/ui/island";
import { Badge, Button } from "frappe-ui";
import { List, ListCell, ListHeader, ListHeaderCell, ListRow, ListRows } from "frappe-ui/list";
import { computed, watch } from "vue";

import { CARD, CARD_TITLE, CELL, LINK, MUTED, NUM, count, day, usd, useCall } from "./lib.js";

const props = defineProps({ params: Object, version: Number });
const emit = defineEmits(["filter"]);
const host = useHost();
const { data, loading, error, load } = useCall("teams");

watch(
	() => [props.params, props.version],
	() => load({ filters: props.params }),
	{ immediate: true }
);

const COLUMNS = ["minmax(0,1.4fr)", "7rem", "4.5rem", "minmax(0,1.2fr)", "5rem", "6rem", "6rem", "2.5rem"];
const rows = computed(() => (data.value || []).map((row) => ({ ...row, id: row.team })));

const limitPct = (row) =>
	row.spend_limit ? Math.min(100, (row.month_spent / row.spend_limit) * 100) : null;
const barClass = (pct) =>
	pct >= 100 ? "bg-surface-red-5" : pct >= 80 ? "bg-surface-amber-3" : "bg-surface-gray-7";
const open = (row) => host.navigate?.(["Form", "Grove Team", row.team]);
</script>

<template>
	<section :class="CARD">
		<header class="mb-2 flex items-center justify-between gap-3">
			<h2 :class="CARD_TITLE">Teams</h2>
			<span :class="MUTED">{{ rows.length }} in this window</span>
		</header>
		<p v-if="error" class="text-sm text-ink-red-6">{{ error }}</p>
		<p v-else-if="loading && !data" :class="MUTED">Loading…</p>
		<p v-else-if="!rows.length" :class="MUTED">No team was served in this window.</p>
		<List v-else :columns="COLUMNS" :row-height="44">
			<ListHeader>
				<ListHeaderCell>Team</ListHeaderCell>
				<ListHeaderCell>Tier</ListHeaderCell>
				<ListHeaderCell class="justify-end">Keys</ListHeaderCell>
				<ListHeaderCell>This month</ListHeaderCell>
				<ListHeaderCell>Last used</ListHeaderCell>
				<ListHeaderCell class="justify-end">Requests</ListHeaderCell>
				<ListHeaderCell class="justify-end">Cost</ListHeaderCell>
				<ListHeaderCell />
			</ListHeader>
			<ListRows :items="rows" v-slot="{ item, value }">
				<ListRow :value="value">
					<ListCell class="gap-2">
						<button
							type="button"
							:class="`${LINK} font-medium`"
							:title="`Filter the page to ${item.team}`"
							@click="emit('filter', { team: item.team })"
						>
							{{ item.team }}
						</button>
						<Badge v-if="item.suspended" theme="red" label="Suspended" />
					</ListCell>
					<ListCell :class="CELL">{{ item.tier || "(default)" }}</ListCell>
					<ListCell :class="NUM">
						{{ item.keys_live }}<span class="text-ink-gray-5">/{{ item.max_keys || "∞" }}</span>
					</ListCell>
					<ListCell class="flex-col !items-start justify-center gap-1">
						<span class="text-sm tabular-nums text-ink-gray-5">
							<span class="text-ink-gray-9">{{ usd(item.month_spent) }}</span>
							<template v-if="item.spend_limit"> / {{ usd(item.spend_limit) }}</template>
							<template v-else-if="item.key_cap_window === 'Total'"> · caps for life</template>
						</span>
						<span
							v-if="limitPct(item) !== null"
							class="block h-1 w-full overflow-hidden rounded-full bg-surface-gray-3"
						>
							<span
								class="block h-full rounded-full"
								:class="barClass(limitPct(item))"
								:style="{ width: `${limitPct(item)}%` }"
							/>
						</span>
					</ListCell>
					<ListCell :class="CELL">{{ day(item.last_used) }}</ListCell>
					<ListCell :class="NUM">{{ count(item.requests) }}</ListCell>
					<ListCell :class="`${NUM} font-medium text-ink-gray-9`">{{ usd(item.cost) }}</ListCell>
					<ListCell class="justify-end">
						<Button
							variant="ghost"
							size="sm"
							icon="lucide-arrow-up-right"
							:aria-label="`Open ${item.team}`"
							@click="open(item)"
						/>
					</ListCell>
				</ListRow>
			</ListRows>
		</List>
	</section>
</template>
