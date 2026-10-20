// Copyright (c) 2026, developers@frappe.io and contributors
// For license information, please see license.txt

function pick_models(frm, answer) {
	if (!answer.new.length) {
		frappe.msgprint(
			__("{0} lists {1} models and this record holds every one.", [
				frm.doc.provider_name,
				answer.held.length,
			])
		);
		return;
	}
	const dialog = new frappe.ui.Dialog({
		title: __("Add Models from {0}", [frm.doc.provider_name]),
		fields: [
			{
				fieldname: "model_ids",
				fieldtype: "MultiCheck",
				columns: 2,
				select_all: true,
				label: __("{0} not yet held ({1} already are)", [
					answer.new.length,
					answer.held.length,
				]),
				options: answer.new.map((id) => ({ label: id, value: id })),
			},
		],
		primary_action_label: __("Next"),
		primary_action(values) {
			dialog.hide();
			name_models(frm, values.model_ids);
		},
	});
	dialog.show();
}

function name_models(frm, upstream_ids) {
	const dialog = new frappe.ui.Dialog({
		title: __("Model IDs"),
		size: "large",
		fields: [
			{
				fieldname: "models",
				fieldtype: "Table",
				cannot_add_rows: true,
				in_place_edit: true,
				description: __(
					"Our id is what clients send; the vendor id is what the vendor is asked for."
				),
				data: upstream_ids.map((id) => ({
					upstream_model_id: id,
					model_id: id.replaceAll("/", "-"),
				})),
				fields: [
					{
						fieldname: "upstream_model_id",
						fieldtype: "Data",
						label: __("Vendor ID"),
						read_only: 1,
						in_list_view: 1,
					},
					{
						fieldname: "model_id",
						fieldtype: "Data",
						label: __("Model ID"),
						reqd: 1,
						in_list_view: 1,
					},
				],
			},
		],
		primary_action_label: __("Add"),
		primary_action(values) {
			dialog.hide();
			const model_ids = Object.fromEntries(
				values.models.map((row) => [row.upstream_model_id, row.model_id])
			);
			frm.call("add_models", { model_ids }).then((r) => {
				frappe.msgprint(__("Added {0} unpublished models.", [r.message.length]));
			});
		},
	});
	dialog.show();
}

frappe.ui.form.on("Model Provider", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.is_self_hosted || !(frm.doc.api_keys || []).length) {
			return;
		}
		frm.add_custom_button(__("Fetch Models"), () =>
			frm.call("fetch_models").then((r) => {
				pick_models(frm, r.message);
			})
		);
	},
});
