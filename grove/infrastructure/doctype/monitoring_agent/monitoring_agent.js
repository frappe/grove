frappe.ui.form.on("Monitoring Agent", {
	refresh(frm) {
		if (frm.is_new()) return;

		if (frm.doc.status !== "Terminated") grove.add_archive_button(frm);
		if (!frm.doc.machine) return;

		if (frm.doc.status === "Active") {
			frm.add_custom_button(__("Update Config"), () =>
				grove.confirm_call(
					frm,
					__("Rewrite {0}'s vmagent config and restart it?", [frm.doc.name]),
					"update_config"
				)
			);
			frm.add_custom_button(__("Rotate Token"), () => {
				frappe.prompt(
					{
						fieldname: "token",
						fieldtype: "Password",
						label: __("New Metrics Token"),
						reqd: 1,
						description: __(
							"Minted by the ingestion service. Saved here and written to the box in one go — pushes fail until both match."
						),
					},
					({ token }) =>
						frm.call("rotate_metrics_token", { token }).then(() => frm.reload_doc()),
					__("Rotate Metrics Token"),
					__("Rotate")
				);
			});
		} else {
			frm.add_custom_button(__("Install"), () =>
				grove.confirm_call(
					frm,
					__("Install the monitoring agent on {0}?", [frm.doc.name]),
					"setup"
				)
			);
		}

		// Only a dev site needs it: the box cannot reach this Grove, so http_sd never re-fetches.
		if (frappe.boot.developer_mode && frm.doc.status === "Active") {
			frm.add_custom_button(__("Push Targets"), () =>
				grove.confirm_call(
					frm,
					__("Replace {0}'s target list with this Grove's?", [frm.doc.name]),
					"push_targets"
				)
			);
		}

		frm.add_custom_button(__("Show Targets"), () => {
			window.open(
				`/api/method/grove.monitoring.targets?agent=${encodeURIComponent(
					frm.doc.name
				)}&kind=engine`,
				"_blank"
			);
		});
	},
});
