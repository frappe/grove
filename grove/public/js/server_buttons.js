frappe.provide("grove");

grove.add_agent_buttons = function (frm, group) {
	frm.add_custom_button(
		__("Deploy Latest Agent"),
		() =>
			grove.confirm_call(
				frm,
				__("Ship the pathway release from Grove Settings to {0} and restart its agent?", [
					frm.doc.name,
				]),
				"deploy_agent"
			),
		group
	);
	frm.add_custom_button(
		__("Update Memory Limit"),
		() =>
			frappe.prompt(
				{
					fieldname: "limit_mb",
					fieldtype: "Int",
					label: __("Memory Limit (MB)"),
					default: frm.doc.memory_limit_mb || 0,
					description: __(
						"0 is automatic: the box's RAM less 512 MB. The cap applies at once; the Go limit, 90% of it, at pathway's next restart."
					),
				},
				({ limit_mb }) => frm.call("set_memory_limit", { limit_mb }),
				__("Cap pathway on {0}", [frm.doc.name]),
				__("Update")
			),
		group
	);
};

grove.add_tls_buttons = function (frm, certificate_message) {
	frm.add_custom_button(
		__("Sync DNS Records"),
		() =>
			grove.confirm_call(
				frm,
				__("Rewrite {0}'s DNS records to its current address?", [frm.doc.name]),
				"sync_dns_records"
			),
		__("TLS")
	);
	frm.add_custom_button(
		__("Deploy Fleet Certificate"),
		() =>
			grove.confirm_call(
				frm,
				certificate_message ||
					__("Push the Geography's certificate to {0} and reload what serves it?", [
						frm.doc.name,
					]),
				"deploy_tls"
			),
		__("TLS")
	);
};

grove.add_vector_button = function (frm) {
	if (frm.doc.is_vector_setup) {
		frm.add_custom_button(__("Update Vector"), () =>
			grove.confirm_call(
				frm,
				__(
					"Re-render Vector on {0}? It pushes logs — and host metrics when Ship Host Metrics is ticked — to Region {1}'s datum.",
					[frm.doc.name, frm.doc.region]
				),
				"install_vector"
			)
		);
	} else {
		frm.add_custom_button(__("Migrate to Vector"), () =>
			grove.confirm_call(
				frm,
				__(
					"Migrate {0} to Vector? It installs Vector, pushing logs — and host metrics when Ship Host Metrics is ticked — to Region {1}'s datum, then removes node_exporter and the scrape htpasswd.",
					[frm.doc.name, frm.doc.region]
				),
				"migrate_to_vector"
			)
		);
	}
};

grove.add_maintenance_button = function (frm, start_message, group) {
	const label = frm.doc.is_in_maintenance ? __("End Maintenance") : __("Start Maintenance");
	frm.add_custom_button(label, () => grove.toggle_maintenance(frm, start_message), group);
};
