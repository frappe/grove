frappe.ui.form.on("Model Replica", {
	async fetch_engine_logs(frm) {
		grove.stop_log_stream(frm, "stop_engine_logs");
		grove.show_logs(frm, "engine_logs", []);
		const { message } = await frm.call("get_engine_logs", { lines: frm.doc.log_lines || 200 });
		const text = message || __("Nothing came back — the engine has not started on the box.");
		grove.show_logs(frm, "engine_logs", text.split("\n"));
	},

	toggle_log_stream(frm) {
		if (frm.log_streaming) {
			grove.stop_log_stream(frm, "stop_engine_logs");
		} else {
			grove.show_logs(frm, "engine_logs", []);
			grove.start_log_stream(frm, "stream_engine_logs");
		}
	},

	refresh(frm) {
		if (frm.is_new()) return;
		grove.setup_log_view(frm, "engine_logs");
		if (!(frm.doc.model && frm.doc.inference_server)) return;
		const status = frm.doc.status;
		const served = status === "Active" || status === "Broken";
		if (!["Provisioning", "Terminated", "Active"].includes(status)) {
			frm.add_custom_button(__("Deploy"), () => {
				frm.call("setup").then(() => frm.reload_doc());
			});
		}
		if (served || status === "Provisioning") {
			frm.add_custom_button(__("Update Engine Config"), () =>
				grove.confirm_call(
					frm,
					status === "Provisioning"
						? __(
								"Restart the engine with the current config while its deploy is still running? The deploy keeps waiting for the engine to answer, and will pass or fail on this one."
						  )
						: __(
								"Restart the engine to apply config changes? In-flight requests will be dropped."
						  ),
					"apply_engine_config"
				)
			);
		}
		if (served) {
			frm.add_custom_button(__("Stop"), () =>
				grove.confirm_call(
					frm,
					__(
						"Stop this engine? It stops serving and stays on the box until you start it again."
					),
					"stop"
				)
			);
		}
		if (status === "Inactive") {
			frm.add_custom_button(__("Start"), () => {
				frm.call("start").then(() => frm.reload_doc());
			});
		}
		// Not while Provisioning: it would race the play.
		if (served || status === "Inactive") {
			frm.add_custom_button(__("Tear Down"), () =>
				grove.confirm_call(
					frm,
					__(
						"Stop and remove this instance from its box? The model stops serving here, and this deployment cannot be redeployed — make a new one."
					),
					"teardown"
				)
			).addClass("btn-danger");
		}
	},
});
