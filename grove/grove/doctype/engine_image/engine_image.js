frappe.ui.form.on('Engine Image', {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__('Fetch Size'), () => {
			frm.call('fetch_size').then(() => frm.reload_doc());
		});
	},
});
