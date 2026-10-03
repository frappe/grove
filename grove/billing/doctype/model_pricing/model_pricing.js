// Copyright (c) 2026, developers@frappe.io and contributors
// For license information, please see license.txt

frappe.ui.form.on('Model Pricing', {
	refresh(frm) {
		// Once enabled the server refuses every edit; Duplicate is how it is priced again.
		if (frm.is_new() || !frm.doc.enabled_on) return;
		frm.set_read_only();
		frm.disable_save();
	},
});
