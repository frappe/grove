<script setup>
// The Ops page: one filter row every panel below re-reads on, the filters mirrored into the
// URL so a link lands on the same view. Desk draws the head; this island reports its title.
import { useHost } from "@framework/ui/island";
import { Button, Select } from "frappe-ui";
import { computed, onMounted, reactive, ref, watch } from "vue";

import GatewayCheck from "./GatewayCheck.vue";
import Gateways from "./Gateways.vue";
import Keys from "./Keys.vue";
import KeyStats from "./KeyStats.vue";
import Leaders from "./Leaders.vue";
import Overview from "./Overview.vue";
import Teams from "./Teams.vue";
import Trend from "./Trend.vue";
import { useCall } from "./lib.js";

const props = defineProps({
	route: { type: Array, default: () => [] },
	query: { type: Object, default: () => ({}) },
});
const emit = defineEmits(["title", "actions"]);
const host = useHost();

const PERIODS = [
	"Today",
	"Yesterday",
	"Last 7 Days",
	"Last 30 Days",
	"This Month",
	"Last Month",
	"Custom",
];
const DEFAULTS = {
	period: "This Month",
	from_date: "",
	to_date: "",
	team: "",
	api_key: "",
	model: "",
	provider: "",
	geography: "",
};
const INPUT =
	"h-7 rounded border border-outline-gray-2 bg-surface-white px-2 text-base text-ink-gray-8 placeholder:text-ink-gray-4 focus:outline-none focus:ring-2 focus:ring-outline-gray-3";

const filters = reactive({ ...DEFAULTS, ...fromQuery(props.query) });
const version = ref(0);

function fromQuery(query) {
	return Object.fromEntries(
		Object.keys(DEFAULTS)
			.filter((key) => query[key])
			.map((key) => [key, query[key]])
	);
}

const isFiltered = computed(() =>
	Object.keys(DEFAULTS).some((key) => (filters[key] || "") !== DEFAULTS[key])
);
const ready = computed(
	() => filters.period !== "Custom" || Boolean(filters.from_date && filters.to_date)
);
const params = computed(() => ({ ...filters }));

// Every picker is a Select off one `filter_options` read. A searching Link (frappe-ui's Combobox)
// closes on the option's focus inside a shadow root (reka-ui reads `document.activeElement`), so
// none is used here until reka-ui reads the root node; the lists are short enough to scroll.
const options = useCall("filter_options");
const choices = computed(() => options.data.value || {});
const teamOptions = computed(() => [
	{ label: "All teams", value: "" },
	...(choices.value.teams || []).map((name) => ({ label: name, value: name })),
]);
const geographyOptions = computed(() => [
	{ label: "All geographies", value: "" },
	...(choices.value.geographies || []).map((row) => ({
		label: row.label || row.name,
		value: row.name,
	})),
]);
const keyOptions = computed(() => [
	{ label: "All keys", value: "" },
	...(choices.value.keys || []).map((key) => ({
		label: `${key.title || key.masked}${key.status === "revoked" ? " (revoked)" : ""}`,
		value: key.name,
	})),
]);
const modelOptions = computed(() => {
	const first = new Map();
	for (const model of choices.value.models || []) {
		if (!first.has(model.model_key)) first.set(model.model_key, model.name);
	}
	return [
		{ label: "All models", value: "" },
		...[...first].map(([key, name]) => ({ label: key, value: name })),
	];
});
const providerOptions = computed(() => [
	{ label: "All providers", value: "" },
	...(choices.value.providers || []).map((name) => ({ label: name, value: name })),
]);

watch(
	() => filters.team,
	(team, before) => {
		if (before !== undefined && filters.api_key) filters.api_key = "";
		options.load({ team: team || null });
	}
);

const refresh = () => version.value++;
const reset = () => Object.assign(filters, DEFAULTS);
const apply = (change) => Object.assign(filters, change);

watch(filters, () => {
	const search = new URLSearchParams(
		Object.entries(filters).filter(([key, value]) => value && value !== DEFAULTS[key])
	);
	const url = `${window.location.pathname}${search.size ? `?${search}` : ""}`;
	window.history.replaceState(window.history.state, "", url);
});

onMounted(() => {
	emit("title", "Ops");
	emit("actions", [{ label: "Refresh", onClick: refresh }]);
	options.load({ team: filters.team || null });
});
</script>

<template>
	<div class="h-full overflow-y-auto bg-surface-gray-1 p-4 md:p-5">
		<div class="mx-auto flex max-w-screen-2xl flex-col gap-4">
			<div class="flex flex-wrap items-center gap-2">
				<Select
					v-model="filters.period"
					:options="PERIODS"
					side="bottom"
					align="start"
					aria-label="Period"
				>
					<template #prefix><span class="text-ink-gray-5">Period</span></template>
				</Select>
				<template v-if="filters.period === 'Custom'">
					<input v-model="filters.from_date" type="date" aria-label="From" :class="INPUT" />
					<span class="text-sm text-ink-gray-5">to</span>
					<input v-model="filters.to_date" type="date" aria-label="To" :class="INPUT" />
				</template>
				<Select
					v-model="filters.team"
					:options="teamOptions"
					side="bottom"
					align="start"
					aria-label="Team"
				>
					<template #prefix><span class="text-ink-gray-5">Team</span></template>
				</Select>
				<Select
					v-model="filters.api_key"
					:options="keyOptions"
					:disabled="!filters.team"
					side="bottom"
					align="start"
					aria-label="API key"
				>
					<template #prefix><span class="text-ink-gray-5">Key</span></template>
				</Select>
				<Select
					v-model="filters.model"
					:options="modelOptions"
					side="bottom"
					align="start"
					aria-label="Model"
				>
					<template #prefix><span class="text-ink-gray-5">Model</span></template>
				</Select>
				<Select
					v-model="filters.provider"
					:options="providerOptions"
					side="bottom"
					align="start"
					aria-label="Provider"
				>
					<template #prefix><span class="text-ink-gray-5">Provider</span></template>
				</Select>
				<Select
					v-model="filters.geography"
					:options="geographyOptions"
					side="bottom"
					align="start"
					aria-label="Geography"
				>
					<template #prefix><span class="text-ink-gray-5">Geography</span></template>
				</Select>
				<Button v-if="isFiltered" variant="ghost" label="Reset filters" @click="reset" />
				<Button
					class="ml-auto"
					variant="subtle"
					icon="lucide-refresh-cw"
					aria-label="Refresh"
					@click="refresh"
				/>
			</div>

			<template v-if="ready">
				<Overview :params="params" :version="version" />
				<Trend :params="params" :version="version" />
				<Leaders :params="params" :version="version" @filter="apply" />
				<Teams :params="params" :version="version" @filter="apply" />
				<Keys :params="params" :version="version" @filter="apply" />
				<GatewayCheck :team="filters.team || ''" />
				<Gateways :version="version" />
				<KeyStats :params="params" :version="version" />
			</template>
			<p v-else class="text-sm text-ink-gray-5">Pick both dates of the custom window.</p>
			<p v-if="host.user" class="sr-only">Signed in as {{ host.user }}</p>
		</div>
	</div>
</template>
