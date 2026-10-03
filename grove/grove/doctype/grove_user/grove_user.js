// Copyright (c) 2026, developers@frappe.io and contributors
// For license information, please see license.txt

frappe.ui.form.on('Grove User', {
	setup(frm) {
		// A group grants what its own geography serves.
		frm.set_query('model_groups', () => ({ filters: { geography: frm.doc.geography } }));
	},
});
