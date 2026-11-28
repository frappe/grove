// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Cost Discrepancy", {
	refresh(frm) {
		if (frm.doc.resolution || !frappe.user.has_role("System Manager")) return;
		const figures = __("Gateway charged {0}; Grove priced {1}.", [
			format_currency(frm.doc.gateway_value, null, 9),
			format_currency(frm.doc.grove_value, null, 9),
		]);
		frm.add_custom_button(
			__("Grove is wrong"),
			() =>
				grove.confirm_call(
					frm,
					`${figures} ${__(
						"Land a correction of {1} on key {0} so Grove's spend matches the gateway's?",
						[frm.doc.api_key, format_currency(frm.doc.delta, null, 9)]
					)}`,
					"correct_grove"
				),
			__("Resolve")
		);
		frm.add_custom_button(
			__("Gateway is wrong"),
			() =>
				grove.confirm_call(
					frm,
					`${figures} ${__(
						"Move the gateway's spend for key {0} on {1} by {2} so its balance matches Grove's? It goes out on the next sync, within a minute.",
						[
							frm.doc.api_key,
							frm.doc.gateway_store,
							format_currency(-frm.doc.delta, null, 9),
						]
					)}`,
					"correct_gateway"
				),
			__("Resolve")
		);
	},
});
