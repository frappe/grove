frappe.provide("grove");

// Asks for the instance type out of the ones the box can move to — its architecture is fixed by
// its image — then opens the Server Resize made for it, where Start runs it.
grove.prompt_resize = async function (frm) {
	const {
		message: { current, options },
	} = await frm.call("resize_options");
	frappe.prompt(
		{
			fieldname: "instance_type",
			fieldtype: "Autocomplete",
			label: __("Instance Type"),
			reqd: 1,
			options: grove.resize_choices(options),
			description: __(
				"Currently {0} ({1}, {2}). Makes a Server Resize for {3}; Start on it stops the box for a few minutes — a gateway drains first, a store puts its gateways in maintenance.",
				[current.instance_type, current.cpu_architecture, current.size, frm.doc.name]
			),
		},
		async ({ instance_type }) => {
			const { message } = await frm.call("resize", { instance_type });
			frappe.set_route("Form", "Server Resize", message);
		},
		__("Resize {0}", [frm.doc.name]),
		__("Create")
	);
};

grove.resize_choices = function (options) {
	return options.map((type) => ({ value: type.instance_type, description: type.size }));
};
