<script setup>
// Usage per UTC day: cost stacked by a dimension, requests and tokens summed over it.
import { Select } from "frappe-ui";
import { AreaChart, BarChart } from "frappe-ui/charts";
import { computed, ref, watch } from "vue";

import { CARD, CARD_TITLE, compact, useCall } from "./lib.js";

const props = defineProps({ params: Object, version: Number });
const DIMENSIONS = [
	{ label: "by model", value: "model" },
	{ label: "by provider", value: "provider" },
	{ label: "by team", value: "team" },
	{ label: "by geography", value: "geography" },
];
const dimension = ref("model");
const { data, loading, error, load } = useCall("trend");

watch(
	() => [props.params, props.version, dimension.value],
	() => load({ filters: props.params, dimension: dimension.value }),
	{ immediate: true }
);

const rows = computed(() =>
	(data.value || []).map((row) => ({ ...row, label: row.label || "(none)" }))
);

const daily = computed(() => {
	const byDay = new Map();
	for (const row of rows.value) {
		const total = byDay.get(row.day) || { day: row.day, requests: 0, tokens: 0 };
		total.requests += row.requests;
		total.tokens += row.tokens;
		byDay.set(row.day, total);
	}
	return [...byDay.values()];
});

const money = (value) => `$${compact(value)}`;
const pending = computed(() => loading.value && !data.value);
</script>

<template>
	<section :class="`${CARD} flex flex-col gap-3`">
		<header class="flex items-center justify-between gap-3">
			<h2 :class="CARD_TITLE">Per day</h2>
			<Select v-model="dimension" :options="DIMENSIONS" side="bottom" align="end" />
		</header>
		<p v-if="error" class="text-sm text-ink-red-6">{{ error }}</p>
		<div class="h-72">
			<BarChart
				title="Cost"
				:data="rows"
				x="day"
				y="cost"
				split-by="label"
				:max-series="6"
				stacked
				:y-axis="{ min: 0, format: money }"
				:loading="pending"
			/>
		</div>
		<div class="grid gap-3 md:grid-cols-2">
			<div class="h-56">
				<AreaChart
					title="Requests"
					:data="daily"
					x="day"
					y="requests"
					smooth
					:y-axis="{ min: 0, format: compact }"
					:loading="pending"
				/>
			</div>
			<div class="h-56">
				<AreaChart
					title="Tokens"
					:data="daily"
					x="day"
					y="tokens"
					smooth
					:y-axis="{ min: 0, format: compact }"
					:loading="pending"
				/>
			</div>
		</div>
	</section>
</template>
