frappe.ui.form.on('Network', {
	setup(frm) {
		frm.set_query('region', () => ({
			filters: frm.doc.provider_type
				? {cloud_provider: frm.doc.provider_type}
				: {cloud_provider: ['is', 'not set']},
		}));
	},

	refresh(frm) {
		if (frm.is_new()) return;

		if (!frm.doc.vpc_id) {
			frm.add_custom_button(__('Create Network'), () => frm.call('create_network').then(() => frm.reload_doc()));
		}

		if (frm.doc.vpc_id && !(frm.doc.proxy_security_group_ids && frm.doc.inference_security_group_ids && frm.doc.store_security_group_ids)) {
			frm.add_custom_button(__('Create Security Groups'), () => frm.call('create_security_groups').then(() => frm.reload_doc()));
		}

		if (frm.doc.inference_security_group_ids || frm.doc.store_security_group_ids) {
			frm.add_custom_button(__('Sync Ingress Rules'), () => frm.call('sync_inference_ingress'));
		}
	},
});
