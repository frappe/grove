// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

// The entry's own keys first; the counters follow in table order, labelled by the Usage Counter.
const HEAD_COLUMNS = [['model', 'Model'], ['pricing', 'Pricing'], ['requests', 'Requests']];
// In the detail for the reports to sum, not shown: the record's Cost is the figure to read.
const HIDDEN = ['grove_cost'];
// Columns whose value names a document: shown as a link to it. `model` is a key, not a doc name —
// one key may be a doc per geography — so it stays text.
const LINKS = { pricing: 'Model Pricing' };
const CELL = 'white-space: nowrap;';
// The first column stays in view while the rest scrolls under it.
const PINNED = `${CELL} position: sticky; left: 0; background: var(--fg-color);`;

async function counter_columns() {
	const counters = await frappe.db.get_list('Usage Counter', { fields: ['name', 'label'], order_by: 'creation', limit: 0 });
	return counters.filter((c) => c.name !== 'request_count').map((c) => [c.name, c.label || c.name]);
}

function usage_columns(rows, counters) {
	// A key the entries carry that the table does not list is a counter since removed: shown under its own name.
	const listed = [...HEAD_COLUMNS, ...counters];
	const known = [...listed.map(([key]) => key), ...HIDDEN];
	const added = [...new Set(rows.flatMap((row) => Object.keys(row)))].filter((key) => !known.includes(key));
	return [...listed.map(([key, label]) => [key, __(label)]), ...added.map((key) => [key, key])];
}

function usage_value(key, value) {
	const text = frappe.utils.escape_html(String(value ?? ''));
	return LINKS[key] && value ? `<a href="${frappe.utils.get_form_link(LINKS[key], value)}">${text}</a>` : text;
}

function usage_table(rows, counters) {
	const columns = usage_columns(rows, counters);
	const cell = (tag, html, index) => `<${tag} style="${index ? CELL : PINNED}">${html}</${tag}>`;
	const head = columns.map(([, label], index) => cell('th', frappe.utils.escape_html(label), index)).join('');
	const body = rows.map((row) =>
		`<tr>${columns.map(([key], index) => cell('td', usage_value(key, row[key]), index)).join('')}</tr>`).join('');
	return `<div style="overflow-x: auto;"><table class="table table-bordered table-sm">
		<thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

frappe.ui.form.on('Usage Record', {
	async refresh(frm) {
		const rows = JSON.parse(frm.doc.usage || '[]');
		frm.get_field('usage_table').$wrapper.html(usage_table(rows, await counter_columns()));
	},
});
