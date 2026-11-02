// Copyright (c) 2026, developers@frappe.io and contributors
// For license information, please see license.txt

frappe.ui.form.on("Grove Team", {
	refresh(frm) {
		if (frm.is_new()) return;
		if (frm.doc.suspended_at_grove) {
			frm.add_custom_button(__("Resume Keys"), () =>
				grove.confirm_call(
					frm,
					__("Lift the suspension? The gateways take this team's keys again on the next sync. Central puts it back on its next sync if the invoice is still unpaid."),
					"set_suspended_at_grove",
					{ suspended: false }
				)
			);
			return;
		}
		frm.add_custom_button(__("Suspend Keys"), () =>
			grove.confirm_call(
				frm,
				__(
					"Suspend every key of this team? The gateways refuse them all on the next sync, whatever their caps, until Resume Keys. Nothing is revoked, and Central's sync cannot lift it."
				),
				"set_suspended_at_grove",
				{ suspended: true }
			)
		);
		frm.change_custom_button_type(__("Suspend Keys"), null, "danger");
	},
});
