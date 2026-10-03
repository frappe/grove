frappe.ui.form.on('Stuck Usage', {
	refresh(frm) {
		frm.disable_form();
		if (frm.doc.resolved || !frappe.user.has_role('System Manager')) return;
		if (frm.doc.dead_line) {
			frm.add_custom_button(__('Mark Resolved'), () => frm.call('mark_resolved').then(() => frm.reload_doc()));
		} else {
			frm.add_custom_button(__('Pull Now'), () => frm.call('pull_now'));
		}
	},
});
