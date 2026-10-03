frappe.ui.form.on('Gateway Store', {
	refresh(frm) {
		if (frm.is_new()) return;

		// Offered until it succeeds: Pending, or Broken to retry.
		if (['Pending', 'Broken'].includes(frm.doc.status)) {
			frm.add_custom_button(__('Setup'), () => grove.confirm_call(frm,
				__('Run the store play on {0}? It installs Redis behind its password on the private address.', [frm.doc.name]),
				'setup'));
		}
		if (frm.doc.status !== 'Terminated') {
			frm.add_custom_button(__('Archive'), () => {
				frappe.confirm(
					__('Archive {0}? Its Machine is terminated — the box and everything on its disk are gone. Refused while any gateway still runs on it.', [frm.doc.name]),
					() => frm.call('archive').then(() => frm.reload_doc()),
				);
			}, __('Danger'));
		}
		if (frm.doc.status === 'Active') {
			frm.add_custom_button(__('Restore'), () => {
				frappe.prompt(
					{
						fieldname: 'source', fieldtype: 'Link', options: 'Gateway Store', label: __('Backup of'),
						default: frm.doc.name, reqd: 1,
						description: __('Itself, or the Terminated store this box replaces. Only for a store whose disk is gone — a surviving AOF already holds everything.'),
					},
					({ source }) => frappe.confirm(
						__('Replace everything in {0}\'s Redis with {1}\'s latest backup? Writes fail for a few seconds while it loads. The current state is uploaded first.', [frm.doc.name, source]),
						() => frm.call('restore', { source }).then(() => frm.reload_doc()),
					),
					__('Restore'),
				);
			}, __('Danger'));
		}
	},
});
