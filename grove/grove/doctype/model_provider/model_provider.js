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

frappe.ui.form.on('Model Provider', {
	refresh(frm) {
		frm.get_field('key_stats_html').$wrapper.empty();
		if (frm.is_new() || frm.doc.is_self_hosted || !(frm.doc.api_keys || []).length) {
			return;
		}
		// On a click, never on open: a form open must not dial the fleet.
		frm.add_custom_button(__('Key Stats'), () => frm.call('key_stats').then((r) => {
			frm.get_field('key_stats_html').$wrapper.html(stats_table(r.message));
		}));
	},
});
