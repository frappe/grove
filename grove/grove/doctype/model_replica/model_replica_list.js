// Without this frappe.utils.guess_colour colours off the status text, and Inactive and Broken
// both come out the same grey.
frappe.listview_settings["Model Replica"] = {
	get_indicator(doc) {
		const colour = {
			Draft: "grey",
			Provisioning: "orange",
			Active: "green",
			Inactive: "darkgrey",
			Broken: "red",
		}[doc.status];
		return [__(doc.status), colour || "grey", `status,=,${doc.status}`];
	},
};
