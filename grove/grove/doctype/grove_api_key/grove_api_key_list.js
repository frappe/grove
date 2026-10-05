frappe.listview_settings['Grove API Key'] = {
	get_indicator(doc) {
		const [label, color] = doc.status === 'active' ? [__('Active'), 'green'] : [__('Revoked'), 'gray'];
		return [label, color, `status,=,${doc.status}`];
	},
};
