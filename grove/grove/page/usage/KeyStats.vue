<script setup>
import { useHost } from "@framework/ui/island";
import { Badge, Button, Select } from "frappe-ui";
import { BarChart } from "frappe-ui/charts";
import { List, ListCell, ListHeader, ListHeaderCell, ListRow, ListRows } from "frappe-ui/list";
import { computed, ref, watch } from "vue";

import {
	CARD,
	CARD_TITLE,
	CELL,
	MUTED,
	NUM,
	ago,
	compact,
	count,
	useCall,
	useFilteredCall,
	when,
} from "./lib.js";

const props = defineProps({ params: Object, version: Number });
const host = useHost();
const { data, loading, error } = useFilteredCall("key_stats", props);
const SPLITS = [
	{ label: "by outcome", value: "outcome" },
	{ label: "by key", value: "key" },
	{ label: "by provider", value: "provider" },
];
const split = ref("outcome");
const {
	data: trendData,
	loading: trendLoading,
	error: trendError,
	load: loadTrend,
} = useCall("key_stats_trend");

watch(
	() => [props.params, props.version, split.value],
	() => loadTrend({ filters: props.params, split: split.value }),
	{ immediate: true }
);

const days = computed(() =>
	(trendData.value || []).map((row) => ({
		...row,
		label: row.label.replace("_", " ") || "(none)",
	}))
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
const COLUMNS = [
	"minmax(0,1.2fr)",
	"minmax(0,1fr)",
	...COUNTS.map(() => "5.5rem"),
	"8rem",
	"2.5rem",
];

// `disabled` is the key's own, so null says the row predates key ids and matched no key.
const rows = computed(() =>
	(data.value?.rows || []).map((row) => ({
		...row,
		id: `${row.model_provider}:${row.key_id}`,
		held: row.disabled !== null,
		throttled: row.last_rate_limited ? `Last throttled ${when(row.last_rate_limited)}` : "",
	}))
);

const lastPull = computed(() => {
	const at = data.value?.last_pull;
	return at ? `${ago(at)} (${when(at)})` : ago(at);
});

const open = (row) => host.navigate?.(["Form", "Model Provider Key", row.key_id]);

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
			<div class="flex flex-wrap items-center gap-x-3 gap-y-1">
				<p :class="MUTED">
					vendor keys · counters pulled every 6 h, summed over the window
				</p>
				<Select
					v-if="rows.length"
					v-model="split"
					:options="SPLITS"
					side="bottom"
					align="end"
					aria-label="Split the chart"
				/>
			</div>
		</header>
		<p v-if="error" class="text-sm text-ink-red-6">{{ error }}</p>
		<p v-else-if="loading && !data" :class="MUTED">Loading…</p>
		<p v-else-if="!rows.length" :class="MUTED">
			No pull landed in this window. Last pull {{ lastPull }}.
		</p>
		<template v-else>
			<p v-if="trendError" class="text-sm text-ink-red-6">{{ trendError }}</p>
			<div class="mb-3 h-64">
				<BarChart
					title="Requests per day"
					:data="days"
					x="day"
					y="requests"
					split-by="label"
					:max-series="6"
					stacked
					palette="categorical"
					:y-axis="{ min: 0, format: compact }"
					:loading="trendLoading && !trendData"
				/>
			</div>
			<div class="overflow-x-auto">
				<List :columns="COLUMNS" :row-height="44" class="min-w-[76rem]">
					<ListHeader>
						<ListHeaderCell>Provider</ListHeaderCell>
						<ListHeaderCell>Key</ListHeaderCell>
						<ListHeaderCell v-for="field in COUNTS" :key="field" class="justify-end">
							{{ field.replace("_", " ") }}
						</ListHeaderCell>
						<ListHeaderCell>Last used</ListHeaderCell>
						<ListHeaderCell />
					</ListHeader>
					<ListRows :items="rows" v-slot="{ item, value }">
						<ListRow :value="value">
							<ListCell class="flex-col !items-start justify-center">
								<span :class="CELL">{{
									item.provider || item.provider_name
								}}</span>
								<span :class="MUTED">{{ item.geography }}</span>
							</ListCell>
							<ListCell class="flex-col !items-start justify-center">
								<span class="flex max-w-full items-center gap-2">
									<span :class="`${CELL} font-medium`">{{
										item.key_title || "Unknown key"
									}}</span>
									<Badge v-if="item.disabled" label="Disabled" />
								</span>
								<span class="font-mono text-xs text-ink-gray-5">{{
									item.key_id
								}}</span>
							</ListCell>
							<ListCell
								v-for="field in COUNTS"
								:key="field"
								:class="`${NUM} ${tone(field, item[field])}`"
							>
								{{ count(item[field]) }}
							</ListCell>
							<ListCell :class="CELL" :title="item.throttled">
								{{ when(item.last_used) }}
							</ListCell>
							<ListCell class="justify-end">
								<Button
									v-if="item.held"
									variant="ghost"
									size="sm"
									icon="lucide-arrow-up-right"
									:aria-label="`Open key ${item.key_title}`"
									@click="open(item)"
								/>
							</ListCell>
						</ListRow>
					</ListRows>
				</List>
			</div>
		</template>
	</section>
</template>
