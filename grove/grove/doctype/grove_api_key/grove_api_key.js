// Copyright (c) 2026, developers@frappe.io and contributors
// For license information, please see license.txt

frappe.ui.form.on('Grove API Key', {
	setup(frm) {
		// A group grants what its own geography serves.
		frm.set_query('model_groups', () => ({ filters: { geography: frm.doc.geography } }));
	},

	refresh(frm) {
		if (frm.is_new() || frm.doc.status !== 'active') return;

		frm.add_custom_button(__('Revoke'), () => {
			frappe.confirm(
				__('Revoke this key? It is kept here as a record, its unspent cap returns to the team, and it is dropped from every gateway on the next sync. It cannot be un-revoked.'),
				() => frm.call('revoke').then(() => frm.reload_doc()),
			);
		});
		frm.change_custom_button_type(__('Revoke'), null, 'danger');
	},
});
