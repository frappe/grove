// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on('Credit Discrepancy', {
	refresh(frm) {
		if (frm.doc.resolved || !frappe.user.has_role('System Manager')) return;
		const figures = __('Gateway charged {0}; Grove priced {1}.', [
			format_currency(frm.doc.gateway_value, null, 9),
			format_currency(frm.doc.grove_value, null, 9),
		]);
		frm.add_custom_button(__('Grove is wrong'), () => {
			frappe.confirm(
				`${figures} ${__("Move {0}'s spent by {1} so Grove's balance matches the gateway's?", [
					frm.doc.grove_user, format_currency(frm.doc.delta, null, 9),
				])}`,
				() => frm.call('correct_grove').then(() => frm.reload_doc()),
			);
		}, __('Resolve'));
		frm.add_custom_button(__('Gateway is wrong'), () => {
			frappe.confirm(
				`${figures} ${__("Move the gateway's spend for {0} on {1} by {2} so its balance matches Grove's?", [
					frm.doc.grove_user, frm.doc.gateway_store, format_currency(-frm.doc.delta, null, 9),
				])}`,
				() => frm.call('correct_gateway').then(() => frm.reload_doc()),
			);
		}, __('Resolve'));
	},
});
