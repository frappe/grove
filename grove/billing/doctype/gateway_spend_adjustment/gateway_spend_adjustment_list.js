frappe.listview_settings['Gateway Spend Adjustment'] = {
	get_indicator(doc) {
		return doc.status === 'Applied'
			? [__('Applied'), 'green', 'status,=,Applied']
			: [__('Pending'), 'orange', 'status,=,Pending'];
	},
};
