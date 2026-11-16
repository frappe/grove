<script setup>
// Each vendor key's counters over the window, as the gateways kept them since the previous
// pull: a key the vendor refuses reads red, one it throttles reads amber.
import { Badge } from "frappe-ui";
import { List, ListCell, ListHeader, ListHeaderCell, ListRow, ListRows } from "frappe-ui/list";
import { computed, watch } from "vue";

import { CARD, CARD_TITLE, CELL, MUTED, NUM, ago, count, useCall, when } from "./lib.js";

const props = defineProps({ params: Object, version: Number });
const { data, loading, error, load } = useCall("key_stats");

watch(
	() => [props.params, props.version],
	() => load({ filters: props.params }),
	{ immediate: true }
);

const COUNTS = [
	"requests",
	"ok",
	"rate_limited",
	"unauthorized",
	"unpaid",
	"forbidden",
	"rejected",
	"failed",
];
const BAD = new Set(["unauthorized", "unpaid", "forbidden"]);
const COLUMNS = ["minmax(0,1.2fr)", "minmax(0,1fr)", ...COUNTS.map(() => "5.5rem"), "8rem"];

const rows = computed(() =>
	(data.value?.rows || []).map((row) => ({
		...row,
		id: `${row.model_provider}:${row.key_title}`,
	}))
);

const tone = (field, value) => {
	if (!Number(value)) return "text-ink-gray-5";
	if (BAD.has(field)) return "font-medium text-ink-red-6";
	if (field === "rate_limited") return "font-medium text-ink-amber-4";
	return "";
};
</script>

<template>
	<section :class="CARD">
		<header class="mb-2 flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
			<h2 :class="CARD_TITLE">Key stats</h2>
			<span :class="MUTED"
				>vendor keys · counters pulled every 6 h, summed over the window</span
			>
		</header>
		<p v-if="error" class="text-sm text-ink-red-6">{{ error }}</p>
		<p v-else-if="loading && !data" :class="MUTED">Loading…</p>
		<p v-else-if="!rows.length" :class="MUTED">
			No pull landed in this window. Last pull {{ ago(data?.last_pull)
			}}<template v-if="data?.last_pull"> ({{ when(data.last_pull) }})</template>.
		</p>
		<div v-else class="overflow-x-auto">
			<List :columns="COLUMNS" :row-height="44" class="min-w-[76rem]">
				<ListHeader>
					<ListHeaderCell>Provider</ListHeaderCell>
					<ListHeaderCell>Key</ListHeaderCell>
					<ListHeaderCell v-for="field in COUNTS" :key="field" class="justify-end">
						{{ field.replace("_", " ") }}
					</ListHeaderCell>
					<ListHeaderCell>Last used</ListHeaderCell>
				</ListHeader>
				<ListRows :items="rows" v-slot="{ item, value }">
					<ListRow :value="value">
						<ListCell class="flex-col !items-start justify-center">
							<span :class="CELL">{{ item.provider || item.provider_name }}</span>
							<span :class="MUTED">{{ item.geography }}</span>
						</ListCell>
						<ListCell class="gap-2">
							<span :class="CELL">{{ item.key_title }}</span>
							<Badge v-if="item.disabled" label="Disabled" />
						</ListCell>
						<ListCell
							v-for="field in COUNTS"
							:key="field"
							:class="`${NUM} ${tone(field, item[field])}`"
						>
							{{ count(item[field]) }}
						</ListCell>
						<ListCell
							:class="CELL"
							:title="
								item.last_rate_limited
									? `Last throttled ${when(item.last_rate_limited)}`
									: ''
							"
						>
							{{ when(item.last_used) }}
						</ListCell>
					</ListRow>
				</ListRows>
			</List>
		</div>
	</section>
</template>
