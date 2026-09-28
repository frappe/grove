frappe.ui.form.on('Gateway Server', {
	refresh(frm) {
		if (frm.is_new()) return;

		// Offered until it succeeds: Pending, or Broken to retry. Deploy Latest Agent covers a live box.
		if (['Pending', 'Broken'].includes(frm.doc.status)) {
			frm.add_custom_button(__('Provision'), () => grove.confirm_call(frm,
				__('Run the full gateway play on {0}? It installs the agent and its config, and starts it.', [frm.doc.name]),
				'setup'), __('Gateway'));
		}

		if (frm.doc.admin_url) {
			frm.add_custom_button(__('Ping'), () => frm.call('ping'), __('Gateway'));
		}

		if (frm.doc.machine) {
			frm.add_custom_button(__('Deploy Latest Agent'), () => grove.confirm_call(frm,
				__('Ship the pathway release from Grove Settings to {0} and restart its agent?', [frm.doc.name]),
				'deploy_agent'), __('Gateway'));


			// The exporters listen on 9100 for the Monitoring Agent above to scrape —
			// restrict that port to that agent in the security group.
			frm.add_custom_button(__('Update Scrape Auth'), () => grove.confirm_call(frm,
				__("Rewrite {0}'s exporter password from Grove Settings? Scrapes with the old one fail from then on.", [frm.doc.name]),
				'update_scrape_auth'));

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
		if (frm.doc.status === 'Active' && frm.doc.admin_url) {
			frm.add_custom_button(__('Check State'), () => {
				frm.call('check_state');
			}, __('Gateway'));

			frm.add_custom_button(__('Full Sync'), () => grove.confirm_call(frm,
				__("Push every key, user, group and route to {0}'s store, whether or not it already holds them?", [frm.doc.name]),
				'full_sync'), __('Gateway'));

			frm.add_custom_button(__('Pull Usage'), () => grove.confirm_call(frm,
				__("Drain {0}'s store now and bill what it holds?", [frm.doc.name]),
				'pull_usage'), __('Gateway'));

			const start = !frm.doc.is_in_maintenance;
			frm.add_custom_button(start ? __('Start Maintenance') : __('End Maintenance'), () => {
				frappe.confirm(
					start
						? __('New requests to {0} get 503 until maintenance ends; running ones finish.', [frm.doc.name])
						: __('{0} serves new requests again.', [frm.doc.name]),
					() => frm.call('set_maintenance', { on: start ? 1 : 0 }).then(() => frm.reload_doc()),
				);
			}, __('Gateway'));
		}
		if (frm.doc.is_in_maintenance) {
			frm.set_intro(__('In maintenance — new requests get 503.'), 'orange');
		}
	},
});
