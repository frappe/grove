frappe.listview_settings['Stuck Usage'] = {
	get_indicator(doc) {
		return doc.resolved ? [__('Resolved'), 'grey', 'resolved,=,1'] : [__('Stuck'), 'red', 'resolved,=,0'];
	},
};
