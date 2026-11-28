frappe.ui.form.on("Pathway Update", {
	setup(frm) {
		frm.set_query("server_type", "servers", () => {
			const server_types = [];
			if (frm.doc.ingresses) server_types.push("Ingress Server");
			if (frm.doc.gateways) server_types.push("Gateway Server");
			return { filters: { name: ["in", server_types] } };
		});
		frm.set_query("server", "servers", () => ({ filters: { status: "Active" } }));
	},

	async onload(frm) {
		if (!frm.is_new()) return;
		const [release, repo] = await Promise.all([
			frappe.db.get_single_value("Grove Settings", "pathway_release"),
			frappe.db.get_single_value("Grove Settings", "pathway_repo"),
		]);
		frm.set_value({ release, repo });
	},

	refresh(frm) {
		if (frm.is_new()) return;
		const status = frm.doc.status;
		// Pending with a start time: Stop was pressed and the current server is still deploying.
		const stopping = status === "Pending" && frm.doc.started_at;
		if (status === "Running" || stopping) {
			frm.disable_save();
		} else {
			frm.enable_save();
		}
		if (frm.is_dirty()) return;

		add_run_buttons(frm, status, stopping);
		show_status_intro(frm, status, stopping);
	},
});

function add_run_buttons(frm, status, stopping) {
	const pending = frm.doc.servers.filter((row) => row.status === "Pending").length;
	if (status === "Pending" && !stopping) {
		const message = __("Deploy pathway {0} to {1} servers, one at a time?", [
			frm.doc.release,
			pending,
		]);
		frm.add_custom_button(__("Start"), () => grove.confirm_call(frm, message, "start"));
	}
	if (["Stopped", "Failure", "Success"].includes(status) && pending) {
		const message = __(
			"Deploy pathway {0} to the {1} Pending servers? Failed rows are passed over.",
			[frm.doc.release, pending]
		);
		frm.add_custom_button(__("Continue"), () => grove.confirm_call(frm, message, "resume"));
	}
	if (status === "Running") {
		const message = __(
			"Stop once the server being deployed is done? Nothing is stopped mid-deploy."
		);
		frm.add_custom_button(__("Stop"), () => grove.confirm_call(frm, message, "stop"));
	}
}

function show_status_intro(frm, status, stopping) {
	if (stopping) {
		frm.set_intro(__("Stopping — the server being deployed finishes first."), "blue");
	} else if (status === "Stopped") {
		frm.set_intro(__("Stopped. Continue carries on with the Pending rows."), "orange");
	} else if (status === "Failure") {
		frm.set_intro(
			__(
				"A deploy failed. The failed server was put in maintenance to debug: End Maintenance on it when done. Continue carries on past it; add its server again as a new row to retry."
			),
			"red"
		);
	}
}
