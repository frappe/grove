// Copyright (c) 2026, developers@frappe.io and contributors
// For license information, please see license.txt

frappe.ui.form.on('Central Team', {
	refresh(frm) {
		if (frm.is_new() || !frappe.user.has_role('System Manager')) return;

		if (frm.doc.free) {
			frm.add_custom_button(__('Mark Paid'), () => markPrepaid(frm));
		} else {
			frm.add_custom_button(__('Mark Free'), () => {
				frappe.confirm(
					__('Mark {0} Free? Its keys are no longer charged or gated; their spend limits stop applying.', [frm.doc.name]),
					() => frm.call('set_free', { free: 1 }).then(() => frm.reload_doc()),
				);
			});
		}
	},
});

// The flip to prepaid is where every live key gets its spend limit: the balance is handed
// out here, not left at 0 for each key to be found blocked later.
async function markPrepaid(frm) {
	const keys = await frappe.db.get_list('Grove API Key', {
		filters: { team: frm.doc.name, status: 'active' },
		fields: ['name', 'title', 'geography', 'spent'],
		order_by: 'creation asc',
	});
	const dialog = new frappe.ui.Dialog({
		title: __('Mark {0} Prepaid', [frm.doc.name]),
		fields: [
			{
				fieldtype: 'HTML',
				options: `<p class="text-muted">${__(
					'Balance {0}. Each live key needs a spend limit above zero; together they may not exceed the balance.',
					[format_currency(frm.doc.balance, null, 2)],
				)}</p>`,
			},
			{
				fieldtype: 'Table',
				fieldname: 'keys',
				label: __('Live keys'),
				cannot_add_rows: true,
				cannot_delete_rows: true,
				in_place_edit: true,
				data: keys.map((key) => ({ key: key.name, title: key.title, geography: key.geography, spent: key.spent, cap: 0 })),
				fields: [
					{ fieldtype: 'Data', fieldname: 'key', label: __('Key'), read_only: 1, in_list_view: 1 },
					{ fieldtype: 'Data', fieldname: 'title', label: __('Title'), read_only: 1, in_list_view: 1 },
					{ fieldtype: 'Data', fieldname: 'geography', label: __('Geography'), read_only: 1, in_list_view: 1 },
					{ fieldtype: 'Currency', fieldname: 'spent', label: __('Spent'), read_only: 1, in_list_view: 1 },
					{ fieldtype: 'Currency', fieldname: 'cap', label: __('Spend limit (USD)'), reqd: 1, in_list_view: 1 },
				],
			},
		],
		primary_action_label: __('Mark Paid'),
		primary_action(values) {
			const caps = (values.keys || []).map((row) => ({ key: row.key, cap: row.cap }));
			frm.call('set_free', { free: 0, caps }).then(() => {
				dialog.hide();
				frm.reload_doc();
			});
		},
	});
	dialog.show();
}
