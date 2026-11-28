<script setup>
import { useHost } from "@framework/ui/island";
import { Badge } from "frappe-ui";
import { List, ListCell, ListHeader, ListHeaderCell, ListRow, ListRows } from "frappe-ui/list";
import { computed, watch } from "vue";

import { CARD, CARD_TITLE, CELL, LINK, MUTED, ago, useCall, when } from "./lib.js";

const props = defineProps({ version: Number });
const host = useHost();
const { data, loading, error, load } = useCall("gateways");

watch(
	() => props.version,
	() => load(),
	{ immediate: true }
);

const COLUMNS = ["minmax(0,2fr)", "8rem", "minmax(0,1.3fr)", "minmax(0,1.3fr)", "5rem"];
const BOX_THEME = { Active: "green", Broken: "red", Pending: "amber", Installing: "amber" };
const rows = computed(() =>
	(data.value?.gateways || []).map((box) => ({
		...box,
		id: box.name,
		placement: placement(box),
		runs: [describeRun("push", box.push), describeRun("pull", box.pull)],
	}))
);

function placement(box) {
	if (!box.gateway_store) return box.geography;
	const writer = box.is_store_writer ? " (writer)" : "";
	return `${box.geography} · ${box.gateway_store}${writer}`;
}

function describeRun(kind, run) {
	return { kind, run, badge: outcome(run), detail: run && runDetail(run) };
}

function outcome(run) {
	if (!run) return { label: "no run", theme: "gray" };
	if (run.success) return { label: "ok", theme: "green" };
	return { label: run.reachable ? "failed" : "unreachable", theme: "red" };
}

function runDetail(run) {
	const detail = run.detail || run.error?.slice(0, 80) || "";
	if (run.duration_ms == null) return detail;
	return `${detail} · ${run.duration_ms} ms`;
}

const open = (box) => host.navigate?.(["Form", "Gateway Server", box.name]);
</script>

<template>
	<section :class="CARD">
		<header class="mb-2 flex flex-wrap items-center gap-2">
			<h2 :class="`${CARD_TITLE} mr-2`">Gateways</h2>
			<Badge v-if="data?.paused.projection" theme="red" label="Pushes paused" />
			<Badge v-if="data?.paused.usage_pull" theme="red" label="Usage pull paused" />
			<p :class="`${MUTED} ml-auto`">
				last push (every minute, hash-gated) · last usage pull (hourly)
			</p>
		</header>
		<p v-if="error" class="text-sm text-ink-red-6">{{ error }}</p>
		<p v-else-if="loading && !data" :class="MUTED">Loading…</p>
		<p v-else-if="!rows.length" :class="MUTED">No gateway.</p>
		<div v-else class="overflow-x-auto">
			<List :columns="COLUMNS" :row-height="44" class="min-w-[64rem]">
				<ListHeader>
					<ListHeaderCell>Gateway</ListHeaderCell>
					<ListHeaderCell>Status</ListHeaderCell>
					<ListHeaderCell>Last push</ListHeaderCell>
					<ListHeaderCell>Last usage pull</ListHeaderCell>
					<ListHeaderCell class="justify-end">Agent</ListHeaderCell>
				</ListHeader>
				<ListRows :items="rows" v-slot="{ item, value }">
					<ListRow :value="value">
						<ListCell class="flex-col !items-start justify-center">
							<button
								type="button"
								:class="`${LINK} max-w-full font-medium`"
								:title="item.name"
								@click="open(item)"
							>
								{{ item.name }}
							</button>
							<span :class="`${MUTED} max-w-full truncate`">{{
								item.placement
							}}</span>
						</ListCell>
						<ListCell class="flex-wrap gap-1">
							<Badge
								:theme="BOX_THEME[item.status] || 'gray'"
								:label="item.status"
							/>
							<Badge
								v-if="item.is_in_maintenance"
								theme="amber"
								label="Maintenance"
							/>
						</ListCell>
						<ListCell
							v-for="{ kind, run, badge, detail } in item.runs"
							:key="kind"
							class="flex-col !items-start justify-center gap-0.5"
							:title="run?.error || ''"
						>
							<span class="flex items-center gap-2">
								<Badge :theme="badge.theme" :label="badge.label" />
								<span v-if="run?.run_at" :class="CELL" :title="when(run.run_at)">{{
									ago(run.run_at)
								}}</span>
							</span>
							<span v-if="run" :class="`${MUTED} max-w-full truncate`">{{
								detail
							}}</span>
						</ListCell>
						<ListCell class="justify-end text-xs text-ink-gray-7">{{
							item.agent_version || "—"
						}}</ListCell>
					</ListRow>
				</ListRows>
			</List>
		</div>
	</section>
</template>
