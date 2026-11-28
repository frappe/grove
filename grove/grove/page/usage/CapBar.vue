<script setup>
import { computed } from "vue";

import { barClass } from "./lib.js";

const props = defineProps({ pct: Number });
const filled = computed(() => Math.min(100, props.pct));
</script>

<template>
	<span
		role="progressbar"
		:aria-valuenow="Math.round(filled)"
		aria-valuemin="0"
		aria-valuemax="100"
		class="block h-1 w-full overflow-hidden rounded-full bg-surface-gray-3"
	>
		<span
			class="block h-full rounded-full"
			:class="barClass(pct)"
			:style="{ width: `${filled}%` }"
		/>
	</span>
</template>
