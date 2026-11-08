frappe.ui.form.on("Server Resize", {
	setup(frm) {
		frm.set_query("server", () => ({ filters: { status: ["!=", "Terminated"] } }));
	},

	refresh(frm) {
		if (frm.doc.status !== "Pending") return;
		frm.set_intro(
			__(
				"Start stops the box for a few minutes — a gateway drains first, a store puts its gateways in maintenance."
			),
			"orange"
		);
		if (frm.is_new()) {
			frm.trigger("server");
			return;
		}
		if (frm.is_dirty()) return;
		frm.add_custom_button(__("Start"), () => {
			frappe.confirm(
				__("Resize {0} from {1} to {2}?", [frm.doc.server, frm.doc.from_instance_type, frm.doc.instance_type]),
				() => frm.call("start").then(() => frm.reload_doc())
			);
		}).addClass("btn-primary");
	},

	server(frm) {
		if (!frm.is_new() || !frm.doc.server) return;
		frappe
			.xcall("run_doc_method", { dt: frm.doc.server_type, dn: frm.doc.server, method: "resize_options" })
			.then(({ current, options }) => {
				frm.set_value("from_instance_type", current.instance_type);
				frm.set_df_property("from_instance_type", "description", `${current.cpu_architecture} · ${current.size}`);
				frm.get_field("instance_type").set_data(
					options.map((type) => ({ value: type.instance_type, description: type.size }))
				);
			});
	},
});
