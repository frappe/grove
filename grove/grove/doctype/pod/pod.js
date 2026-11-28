frappe.ui.form.on("Pod", {
	setup(frm) {
		frm.set_query("model", () => ({ filters: { provider_is_self_hosted: 1 } }));
	},

	refresh(frm) {
		if (frm.is_new()) return;
		grove.setup_log_view(frm, "log_output");

		if (frm.doc.pod_id && frm.doc.provider_type === "runpod") {
			frm.add_web_link(
				`https://console.runpod.io/pods?id=${frm.doc.pod_id}`,
				__("Open in RunPod")
			);
		}

		if (!frm.doc.pod_id && (frm.doc.status === "Pending" || frappe.boot.developer_mode)) {
			frm.add_custom_button(__("Spawn"), () => {
				frm.call("spawn").then(() => frm.reload_doc());
			});
			return;
		}

		frm.add_custom_button(__("Sync"), () => {
			frm.call("sync").then(() => frm.reload_doc());
		});
		if (frm.doc.status === "Stopped") {
			frm.add_custom_button(__("Start"), () => {
				frm.call("start").then(() => frm.reload_doc());
			});
		} else {
			frm.add_custom_button(__("Stop"), () =>
				grove.confirm_call(
					frm,
					__(
						"Stop this pod? The GPU is released (billing stops) but the volume is kept, so Start can resume it."
					),
					"stop"
				)
			);
		}
		frm.add_custom_button(__("Restart"), () =>
			grove.confirm_call(
				frm,
				__(
					"Restart applies this config to the running pod — it resets, keeping its volume, so weights are not re-downloaded. Its ports may move. An edited GPU type or count cannot be applied to a live pod and is refused: that needs Terminate, then Spawn. Continue?"
				),
				"restart"
			)
		);
		frm.add_custom_button(
			__("Terminate"),
			() =>
				grove.confirm_call(
					frm,
					__("Terminate this pod? Frees GPU/disk/billing."),
					"terminate"
				),
			__("Danger")
		);
	},

	toggle_log_stream(frm) {
		if (frm.log_streaming) {
			grove.stop_log_stream(frm, "stop_logs");
		} else {
			grove.start_log_stream(frm, "stream_logs");
		}
	},
});
