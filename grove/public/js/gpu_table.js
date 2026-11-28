frappe.provide("grove");

grove.render_gpu_table = function (wrapper, gpus, opts = {}) {
	if (!wrapper) return;
	if (!gpus.length) {
		wrapper.html(`<p class="text-muted">${opts.empty || __("No GPUs recorded.")}</p>`);
		return;
	}

	const free = gpus.filter((g) => !g.held_by).length;
	const rows = gpus.map(grove.gpu_table_row).join("");
	const note = opts.note ? ` · ${opts.note}` : "";

	wrapper.html(`
		<div class="text-muted" style="margin-bottom:6px">
			${__("{0} of {1} free", [free, gpus.length])}${note}
		</div>
		<table class="table table-bordered" style="margin:0">
			<thead><tr>
				<th style="text-align:right">${__("CUDA")}</th>
				<th>${__("Type")}</th>
				<th style="text-align:right">${__("VRAM GB")}</th>
				<th>${__("Device")}</th>
				<th>${__("Status")}</th>
				<th>${__("Held By")}</th>
			</tr></thead>
			<tbody>${rows}</tbody>
		</table>
	`);
};

grove.gpu_table_row = function (gpu) {
	const esc = frappe.utils.escape_html;
	let status = `<span class="indicator-pill green">${__("Free")}</span>`;
	let holder = `<span class="text-muted">—</span>`;
	if (gpu.held_by) {
		const model = (gpu.deployments || [])[0]?.model || gpu.held_by;
		const href = `/app/model-replica/${encodeURIComponent(gpu.held_by)}`;
		status = `<span class="indicator-pill blue">${__("Held")}</span>`;
		holder = `<a href="${href}">${esc(model)}</a>`;
	}
	return `<tr>
		<td style="text-align:right">${gpu.gpu_index}</td>
		<td>${esc(gpu.gpu_type || "")}</td>
		<td style="text-align:right">${gpu.vram_gb || ""}</td>
		<td><code>${esc(gpu.device_id || "")}</code></td>
		<td>${status}</td>
		<td>${holder}</td>
	</tr>`;
};
