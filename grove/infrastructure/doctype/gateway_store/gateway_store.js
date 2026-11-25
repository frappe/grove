frappe.ui.form.on("Gateway Store", {
	refresh(frm) {
		if (frm.is_new()) return;

		if (["Pending", "Broken"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Setup"), () =>
				grove.confirm_call(
					frm,
					__(
						"Run the store play on {0}? It installs Redis behind its password on the private address.",
						[frm.doc.name]
					),
					"setup"
				)
			);
		}
		if (frm.doc.status === "Active") {
			frm.add_custom_button(__("Sync DNS Records"), () =>
				grove.confirm_call(
					frm,
					__("Rewrite {0}'s DNS record to its current address?", [frm.doc.name]),
					"sync_dns_records"
				)
			);
			frm.add_custom_button(
				frm.doc.is_vector_setup ? __("Update Vector") : __("Install Vector"),
				() =>
					grove.confirm_call(
						frm,
						__(
							"Install Vector on {0}? It pushes logs — and host metrics when Ship Host Metrics is ticked — to Region {1}'s datum.",
							[frm.doc.name, frm.doc.region]
						),
						"install_vector"
					)
			);
		}
		if (frm.doc.status === "Active") {
			frm.add_custom_button(
				__("Upgrade Redis"),
				() =>
					grove.confirm_call(
						frm,
						__(
							"Upgrade Redis on {0}? The store is down while the play swaps it and its gateways refuse requests meanwhile: put them in maintenance and pull usage first. Nothing is stopped when no newer release is out.",
							[frm.doc.name]
						),
						"upgrade_redis"
					),
				__("Danger")
			);
		}
		if (frm.doc.status !== "Terminated") {
			frm.add_custom_button(
				__("Resize Server"),
				() => grove.prompt_resize(frm),
				__("Danger")
			);
			frm.add_custom_button(
				__("Archive"),
				() => {
					frappe.confirm(
						__(
							"Archive {0}? Its Machine is terminated — the box and everything on its disk are gone. Refused while any gateway still runs on it.",
							[frm.doc.name]
						),
						() => frm.call("archive").then(() => frm.reload_doc())
					);
				},
				__("Danger")
			);
		}
	},
});
