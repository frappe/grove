<script setup>
// The window's figures beside the previous window's.
import { NumberCard } from "frappe-ui/charts";
import { computed, watch } from "vue";

import { MUTED, ago, compact, count, signedUsd, usd, useCall } from "./lib.js";

const props = defineProps({ params: Object, version: Number });
const { data, loading, error, load } = useCall("overview");

watch(
	() => [props.params, props.version],
	() => load({ filters: props.params }),
	{ immediate: true }
);

const signedCount = (value) => (value > 0 ? `+${count(value)}` : count(value));
const signedCompact = (value) => (value > 0 ? `+${compact(value)}` : compact(value));

const TILES = [
	{ key: "cost", title: "Cost", format: usd, delta: signedUsd },
	{ key: "drift", title: "Pricing drift", format: usd, delta: signedUsd, worse: true },
	{ key: "requests", title: "Requests", format: count, delta: signedCount },
	{ key: "tokens", title: "Tokens", format: compact, delta: signedCompact },
	{ key: "teams", title: "Teams", format: count, delta: signedCount },
	{ key: "keys", title: "Keys", format: count, delta: signedCount },
];

const totals = computed(() => data.value?.totals || {});
const previous = computed(() => data.value?.previous || {});

const days = computed(() => {
	if (!data.value) return 0;
	const span = new Date(data.value.to_date) - new Date(data.value.from_date);
	return Math.round(span / 86_400_000) + 1;
});
const caption = computed(() => (days.value ? `vs the ${days.value} days before` : ""));

const delta = (key) => {
	const now = totals.value[key];
	const before = previous.value[key];
	return now == null || before == null ? null : now - before;
};
</script>

<template>
	<section class="flex flex-col gap-2">
		<p v-if="error" class="text-sm text-ink-red-6">{{ error }}</p>
		<div class="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
			<NumberCard
				v-for="tile in TILES"
				:key="tile.key"
				:title="tile.title"
				:value="totals[tile.key] ?? null"
				:format="tile.format"
				:delta="delta(tile.key)"
				:delta-format="tile.delta"
				:delta-caption="caption"
				:negative-is-better="tile.worse"
				:loading="loading && !data"
			/>
		</div>
		<p v-if="data" :class="MUTED">
			Drift is Grove's price minus what the boxes charged; anything but $0 is a Cost
			Discrepancy.
			<template v-if="totals.drift === null"
				>Left out under a model or provider filter.</template
			>
			<template v-if="totals.as_of">
				Newest usage in the window pulled {{ ago(totals.as_of) }}.</template
			>
		</p>
	</section>
</template>
