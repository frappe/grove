frappe.ui.form.on('Ingress Server', {
	refresh(frm) {
		if (frm.is_new()) return;

		// Offered until it succeeds: Pending, or Broken to retry. Deploy Latest Agent covers a live box.
		if (['Pending', 'Broken'].includes(frm.doc.status)) {
			frm.add_custom_button(__('Provision'), () => grove.confirm_call(frm,
				__('Run the full ingress play on {0}? It installs the agent and its config, and starts it.', [frm.doc.name]),
				'setup'), __('Ingress'));
		}

		if (frm.doc.admin_url) {
			frm.add_custom_button(__('Ping'), () => frm.call('ping'), __('Ingress'));
		}

		if (frm.doc.status === 'Active' && frm.doc.admin_url) {
			const start = !frm.doc.is_in_maintenance;
			frm.add_custom_button(start ? __('Start Maintenance') : __('End Maintenance'), () => {
				frappe.confirm(
					start
						? __('Requests the gateways send through {0} get 503 until maintenance ends; running ones finish.', [frm.doc.name])
						: __('{0} serves new requests again.', [frm.doc.name]),
					() => frm.call('set_maintenance', { on: start ? 1 : 0 }).then(() => frm.reload_doc()),
				);
			}, __('Ingress'));
		}
		if (frm.doc.is_in_maintenance) {
			frm.set_intro(__('In maintenance — new requests get 503.'), 'orange');
		}

		if (frm.doc.machine) {
			frm.add_custom_button(__('Deploy Latest Agent'), () => grove.confirm_call(frm,
				__('Ship the pathway release from Grove Settings to {0} and restart its agent?', [frm.doc.name]),
				'deploy_agent'), __('Ingress'));


			frm.add_custom_button(__('Update Scrape Auth'), () => grove.confirm_call(frm,
				__("Rewrite {0}'s exporter password from Grove Settings? Scrapes with the old one fail from then on.", [frm.doc.name]),
				'update_scrape_auth'));

			// The replica table: every Active replica in this ingress's Network, dialled privately.
			frm.add_custom_button(__('Sync Replicas'), () => grove.confirm_call(frm,
				__("Replace {0}'s replica table with every Active replica in its Network?", [frm.doc.name]),
				'sync_replicas'), __('Ingress'));

			frm.add_custom_button(__('Sync DNS Records'), () => grove.confirm_call(frm,
				__("Rewrite {0}'s DNS records to its current address?", [frm.doc.name]),
				'sync_dns_records'), __('TLS'));

			frm.add_custom_button(__('Deploy Fleet Certificate'), () => grove.confirm_call(frm,
				__("Push the Geography's certificate to {0} and reload what serves it?", [frm.doc.name]),
				'deploy_tls'), __('TLS'));
		}

		if (frm.doc.status !== 'Terminated') {
			frm.add_custom_button(__('Archive'), () => {
				frappe.confirm(
					__('Archive {0}? Its Machine is terminated — the box and everything on its disk are gone — and this server leaves the fleet. Refused while anything still depends on it.', [frm.doc.name]),
					() => frm.call('archive').then(() => frm.reload_doc()),
				);
			}, __('Danger'));
		}
	},
});
