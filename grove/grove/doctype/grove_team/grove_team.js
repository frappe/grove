// Copyright (c) 2026, developers@frappe.io and contributors
// For license information, please see license.txt

frappe.ui.form.on("Grove Team", {
	refresh(frm) {
		if (frm.is_new()) return;
		if (frm.doc.suspended) {
			frm.add_custom_button(__("Resume Keys"), () =>
				grove.confirm_call(
					frm,
					__("Resume every key of this team? The gateways take them again on the next sync."),
					"set_suspended",
					{ suspended: false }
				)
			);
			return;
		}
		frm.add_custom_button(__("Suspend Keys"), () =>
			grove.confirm_call(
				frm,
				__(
					"Suspend every key of this team? The gateways refuse them all on the next sync, whatever their caps, until Resume Keys. Nothing is revoked. Central overwrites this for its teams on its next sync."
				),
				"set_suspended",
				{ suspended: true }
			)
		);
		frm.change_custom_button_type(__("Suspend Keys"), null, "danger");
	},
});
