frappe.ui.form.on("Ingress Server", {
	refresh(frm) {
		if (frm.is_new()) return;
		add_ingress_buttons(frm);
		if (frm.doc.is_in_maintenance) {
			frm.set_intro(__("In maintenance — new requests get 503."), "orange");
		}
		if (frm.doc.machine) add_machine_buttons(frm);
		if (frm.doc.status === "Active") grove.add_vector_button(frm);
		if (frm.doc.status !== "Terminated") grove.add_archive_button(frm);
	},
});

function add_ingress_buttons(frm) {
	if (["Pending", "Broken"].includes(frm.doc.status)) {
		frm.add_custom_button(
			__("Provision"),
			() =>
				grove.confirm_call(
					frm,
					__(
						"Run the full ingress play on {0}? It installs the agent and its config, and starts it.",
						[frm.doc.name]
					),
					"setup"
				),
			__("Ingress")
		);
	}
	if (!frm.doc.admin_url) return;
	frm.add_custom_button(__("Ping"), () => frm.call("ping"), __("Ingress"));
	if (frm.doc.status === "Active") {
		grove.add_maintenance_button(
			frm,
			__(
				"Requests the gateways send through {0} get 503 until maintenance ends; running ones finish.",
				[frm.doc.name]
			),
			__("Ingress")
		);
	}
}

function add_machine_buttons(frm) {
	grove.add_agent_buttons(frm, __("Ingress"));
	frm.add_custom_button(
		__("Sync Replicas"),
		() =>
			grove.confirm_call(
				frm,
				__("Replace {0}'s replica table with every Active replica in its Network?", [
					frm.doc.name,
				]),
				"sync_replicas"
			),
		__("Ingress")
	);
	grove.add_tls_buttons(frm);
}
