frappe.ui.form.on("Model Deployment", {
	setup(frm) {
		frm.set_query("model", () => ({ filters: { provider_is_self_hosted: 1 } }));
	},

	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Place a Replica"), () => place_replica(frm));
		frm.add_custom_button(__("Add Replica"), () => add_replica(frm));
		show_replicas(frm);
	},
});

// No confirm step, deliberately: the scheduler's "nothing fits" error lands in the same tick as the
// confirm dialog's teardown, the two fight over one backdrop, and the error never showed.
async function place_replica(frm) {
	const { message } = await frm.call({
		doc: frm.doc,
		method: "add_replica",
		freeze: true,
		freeze_message: __("Finding a box…"),
	});
	if (!message) return;
	frappe.show_alert({ message: __("Placed on {0}", [message]), indicator: "green" });
	frappe.set_route("Form", "Model Replica", message);
}

// Any Active box is offered: a deployment is not tied to a region.
function add_replica(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Add Replica"),
		fields: [
			{
				fieldname: "inference_server",
				fieldtype: "Link",
				label: __("Inference Server"),
				options: "Inference Server",
				reqd: 1,
				get_query: () => ({ filters: { status: "Active" } }),
				onchange: () => show_gpu_allocation(dialog),
			},
			{ fieldname: "allocation", fieldtype: "HTML", label: __("GPUs on this box") },
			{
				fieldname: "gpus",
				fieldtype: "Data",
				label: __("GPU Indices"),
				description: __(
					"CUDA indices to pin, comma separated — {0} of them. Blank means single-GPU, unpinned.",
					[frm.doc.gpus_per_replica]
				),
			},
		],
		primary_action_label: __("Add and Deploy"),
		async primary_action({ inference_server, gpus }) {
			dialog.hide();
			const { message } = await frm.call("add_replica", { inference_server, gpus });
			if (message) frappe.set_route("Form", "Model Replica", message);
		},
	});
	dialog.show();
}

async function show_gpu_allocation(dialog) {
	const server = dialog.get_value("inference_server");
	const field = dialog.get_field("allocation");
	if (!server) {
		field.$wrapper.empty();
		return;
	}
	const gpus = await frappe.xcall("run_doc_method", {
		dt: "Inference Server",
		dn: server,
		method: "get_gpu_allocation",
	});
	const rows = (gpus || []).map(gpu_allocation_row).join("");
	field.$wrapper.html(
		`<table class="table table-bordered small"><thead><tr>
		<th>${__("Index")}</th><th>${__("Type")}</th><th>${__("VRAM")}</th>
		<th>${__("Status")}</th><th>${__("Held by")}</th></tr></thead>
		<tbody>${rows}</tbody></table>`
	);
}

function gpu_allocation_row(gpu) {
	const esc = frappe.utils.escape_html;
	const held = (gpu.deployments || []).map((deployment) => deployment.name).join(", ");
	return `<tr><td>${gpu.gpu_index}</td><td>${esc(gpu.gpu_type || "")}</td>
		<td>${gpu.vram_gb || ""}</td><td>${gpu.status}</td>
		<td class="text-muted small">${esc(held)}</td></tr>`;
}

async function show_replicas(frm) {
	const replicas = await frappe.db.get_list("Model Replica", {
		filters: { model_deployment: frm.doc.name },
		fields: ["name", "inference_server", "status"],
		limit: 100,
	});
	const live = replicas.filter((replica) => ["Active", "Provisioning"].includes(replica.status));
	frm.dashboard.clear_headline();
	if (replicas.length) {
		frm.dashboard.set_headline(
			__("{0} replicas — {1} serving or provisioning", [replicas.length, live.length])
		);
	} else {
		frm.dashboard.set_headline(
			__("No replicas yet. Add Replica places one on any Active box.")
		);
	}
}
