frappe.query_reports["Vendor Reconciliation"] = {
	filters: [
		{
			fieldname: "provider",
			label: __("Provider"),
			fieldtype: "Select",
			options: "baseten",
			default: "baseten",
			reqd: 1,
		},
		{
			fieldname: "group_by",
			label: __("Group By"),
			fieldtype: "Select",
			options: "Model\nDay and Model",
			default: "Model",
			reqd: 1,
		},
		{
			fieldname: "from_date",
			label: __("From"),
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.obj_to_str(moment().subtract(1, "days").startOf("month")),
		},
		// Today's figures still move on both sides, so yesterday is the latest day.
		{
			fieldname: "to_date",
			label: __("To"),
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -1),
		},
	],
	formatter(value, row, column, data, default_formatter) {
		const html = default_formatter(value, row, column, data);
		const match = /^(.*)_vendor$/.exec(column.fieldname);
		if (!match || !data) return html;
		const grove = data[`${match[1]}_grove`] || 0;
		const vendor = data[column.fieldname] || 0;
		const tolerance = match[1] === "requests" ? 0 : Math.max(grove, vendor) * 0.01;
		return Math.abs(grove - vendor) > tolerance
			? `<span style="color: var(--red-500)">${html}</span>`
			: html;
	},
};
