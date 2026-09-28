// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

const USAGE_COLUMNS = [
	['model', 'Model'], ['pricing', 'Pricing'], ['requests', 'Requests'],
	['input_tokens', 'Input'], ['cached_tokens', 'Cached'], ['cache_write_tokens', 'Cache write'],
	['cache_write_1h_tokens', 'Cache write 1h'], ['completion_tokens', 'Completion'],
	['audio_seconds', 'Audio s'],
];
// In the detail for the reports to sum, not shown: the record's Cost is the figure to read.
const HIDDEN = ['grove_cost'];
// Columns whose value names a document: shown as a link to it.
const LINKS = { model: 'Model', pricing: 'Model Pricing' };
const CELL = 'white-space: nowrap;';
// The first column stays in view while the rest scrolls under it.
const PINNED = `${CELL} position: sticky; left: 0; background: var(--fg-color);`;

function usage_columns(rows) {
	// A key the entries carry that is not listed is a counter added later: shown under its own name.
	const known = [...USAGE_COLUMNS.map(([key]) => key), ...HIDDEN];
	const added = [...new Set(rows.flatMap((row) => Object.keys(row)))].filter((key) => !known.includes(key));
	return [...USAGE_COLUMNS.map(([key, label]) => [key, __(label)]), ...added.map((key) => [key, key])];
}

function usage_value(key, value) {
	const text = frappe.utils.escape_html(String(value ?? ''));
	return LINKS[key] && value ? `<a href="${frappe.utils.get_form_link(LINKS[key], value)}">${text}</a>` : text;
}

function usage_table(rows) {
	const columns = usage_columns(rows);
	const cell = (tag, html, index) => `<${tag} style="${index ? CELL : PINNED}">${html}</${tag}>`;
	const head = columns.map(([, label], index) => cell('th', frappe.utils.escape_html(label), index)).join('');
	const body = rows.map((row) =>
		`<tr>${columns.map(([key], index) => cell('td', usage_value(key, row[key]), index)).join('')}</tr>`).join('');
	return `<div style="overflow-x: auto;"><table class="table table-bordered table-sm">
		<thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

frappe.ui.form.on('Usage Record', {
	refresh(frm) {
		frm.get_field('usage_table').$wrapper.html(usage_table(JSON.parse(frm.doc.usage || '[]')));
	},
});
