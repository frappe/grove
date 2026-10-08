// Copyright (c) 2026, developers@frappe.io and contributors
// For license information, please see license.txt

const STAT_COLUMNS = [
	['title', 'Key'], ['requests', 'Requests'], ['ok', 'OK'], ['rate_limited', '429'],
	['rejected', 'Rejected'], ['failed', 'Failed'], ['last_used', 'Last Used'],
	['last_rate_limited', 'Last 429'],
];

function stat_value(key, value) {
	if (key.startsWith('last_')) {
		// A unix second from the box; "never" when the key has not answered one of these.
		return value ? moment.unix(value).fromNow() : __('never');
	}
	return frappe.utils.escape_html(String(value ?? ''));
}

function stats_table(answer) {
	const head = STAT_COLUMNS.map(([, label]) => `<th>${__(label)}</th>`).join('');
	const body = answer.keys.map((row) =>
		`<tr>${STAT_COLUMNS.map(([key]) => `<td>${stat_value(key, row[key])}</td>`).join('')}</tr>`).join('');
	const unreached = answer.unreached.length
		? `<p class="text-muted">${__('Not counted — no gateway answered for: {0}', [frappe.utils.escape_html(answer.unreached.join(', '))])}</p>`
		: '';
	return `<table class="table table-bordered table-sm"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>${unreached}`;
}

function pick_models(frm, answer) {
	if (!answer.new.length) {
		frappe.msgprint(__('{0} lists {1} models and this record holds every one.', [frm.doc.provider_name, answer.held.length]));
		return;
	}
	const dialog = new frappe.ui.Dialog({
		title: __('Add Models from {0}', [frm.doc.provider_name]),
		fields: [{
			fieldname: 'model_ids', fieldtype: 'MultiCheck', columns: 2, select_all: true,
			label: __('{0} not yet held ({1} already are)', [answer.new.length, answer.held.length]),
			options: answer.new.map((id) => ({ label: id, value: id })),
		}],
		primary_action_label: __('Next'),
		primary_action(values) {
			dialog.hide();
			name_models(frm, values.model_ids);
		},
	});
	dialog.show();
}

function name_models(frm, upstream_ids) {
	const dialog = new frappe.ui.Dialog({
		title: __('Model IDs'),
		size: 'large',
		fields: [{
			fieldname: 'models', fieldtype: 'Table', cannot_add_rows: true, in_place_edit: true,
			description: __('Our id is what clients send; the vendor id is what the vendor is asked for.'),
			data: upstream_ids.map((id) => ({ upstream_model_id: id, model_id: id.replaceAll('/', '-') })),
			fields: [
				{ fieldname: 'upstream_model_id', fieldtype: 'Data', label: __('Vendor ID'), read_only: 1, in_list_view: 1 },
				{ fieldname: 'model_id', fieldtype: 'Data', label: __('Model ID'), reqd: 1, in_list_view: 1 },
			],
		}],
		primary_action_label: __('Add'),
		primary_action(values) {
			dialog.hide();
			const model_ids = Object.fromEntries(values.models.map((row) => [row.upstream_model_id, row.model_id]));
			frm.call('add_models', { model_ids }).then((r) => {
				frappe.msgprint(__('Added {0} unpublished models.', [r.message.length]));
			});
		},
	});
	dialog.show();
}

frappe.ui.form.on('Model Provider', {
	refresh(frm) {
		frm.get_field('key_stats_html').$wrapper.empty();
		if (frm.is_new() || frm.doc.is_self_hosted || !(frm.doc.api_keys || []).length) {
			return;
		}
		// On a click, never on open: a form open must not dial the fleet or the vendor.
		frm.add_custom_button(__('Key Stats'), () => frm.call('key_stats').then((r) => {
			frm.get_field('key_stats_html').$wrapper.html(stats_table(r.message));
		}));
		frm.add_custom_button(__('Fetch Models'), () => frm.call('fetch_models').then((r) => {
			pick_models(frm, r.message);
		}));
	},
});
