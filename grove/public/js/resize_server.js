frappe.provide("grove");

// Resize Server on a Gateway Server or Gateway Store form: asks for the instance type out of the
// ones the box can move to — its architecture is fixed by its image — then opens the Server
// Resize made for it, where Start runs it.
grove.prompt_resize = function (frm) {
	frm.call("resize_options").then(({ message: { current, options } }) => {
		frappe.prompt(
			{
				fieldname: "instance_type",
				fieldtype: "Autocomplete",
				label: __("Instance Type"),
				reqd: 1,
				options: options.map((type) => ({
					value: type.instance_type,
					description: type.size,
				})),
				description: __(
					"Currently {0} ({1}, {2}). Makes a Server Resize for {3}; Start on it stops the box for a few minutes — a gateway drains first, a store puts its gateways in maintenance.",
					[current.instance_type, current.cpu_architecture, current.size, frm.doc.name]
				),
			},
			({ instance_type }) =>
				frm
					.call("resize", { instance_type })
					.then(({ message }) => frappe.set_route("Form", "Server Resize", message)),
			__("Resize {0}", [frm.doc.name]),
			__("Create")
		);
	});
};
