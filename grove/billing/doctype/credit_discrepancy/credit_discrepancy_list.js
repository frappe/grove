frappe.listview_settings['Credit Discrepancy'] = {
	add_fields: ['gateway_correction_pending'],
	get_indicator(doc) {
		if (doc.gateway_correction_pending) {
			return [__('Pending'), 'orange', 'gateway_correction_pending,=,1'];
		}
		return doc.resolution
			? [__('Resolved'), 'grey', `resolution,is,set`]
			: [__('Open'), 'red', 'resolution,is,not set|gateway_correction_pending,=,0'];
	},
};
