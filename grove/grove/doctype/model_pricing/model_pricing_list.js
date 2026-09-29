frappe.listview_settings['Model Pricing'] = {
	get_indicator(doc) {
		const color = doc.status === 'Enabled' ? 'green' : 'grey';
		return [__(doc.status), color, `status,=,${doc.status}`];
	},
};
