frappe.listview_settings["Credit Discrepancy"] = {
	get_indicator(doc) {
		return doc.resolution
			? [__("Resolved"), "grey", "resolution,is,set"]
			: [__("Open"), "red", "resolution,is,not set"];
	},
};
