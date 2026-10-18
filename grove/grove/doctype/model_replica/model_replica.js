// Twin of pod.js's log viewer, kept per-doctype: doctype JS is served straight off disk, and a
// shared helper would need an asset bundle.
const LOG_LINE_LIMIT = 2000;
// Well inside the server's 45s liveness TTL, so one slow ping doesn't cut the stream.
const LOG_PING_INTERVAL = 15000;

frappe.ui.form.on('Model Replica', {
	fetch_engine_logs(frm) {
		stop_log_stream(frm);
		clear_logs(frm);
		frm.call('get_engine_logs', { lines: frm.doc.log_lines || 200 }).then((r) => {
			const text = r.message || __('Nothing came back — the engine has not started on the box.');
			frm.log_lines = text.split('\n');
			render_logs(frm, true);
		});
	},

	toggle_log_stream(frm) {
		if (frm.log_streaming) {
			stop_log_stream(frm);
		} else {
			clear_logs(frm);
			frm.call('stream_engine_logs').then(() => set_log_button(frm, true));
		}
	},

	refresh(frm) {
		if (frm.is_new()) return;
		setup_log_view(frm);
		if (!(frm.doc.model && frm.doc.inference_server)) return;
		const status = frm.doc.status;
		const served = status === 'Active' || status === 'Broken';
		if (!['Provisioning', 'Terminated', 'Active'].includes(status)) {
			frm.add_custom_button(__('Deploy'), () => {
				frm.call('setup').then(() => frm.reload_doc());
			});
		}
		if (served || status === 'Provisioning') {
			frm.add_custom_button(__('Update Engine Config'), () => {
				frappe.confirm(
					status === 'Provisioning'
						? __('Restart the engine with the current config while its deploy is still running? The deploy keeps waiting for the engine to answer, and will pass or fail on this one.')
						: __('Restart the engine to apply config changes? In-flight requests will be dropped.'),
					() => frm.call('apply_engine_config').then(() => frm.reload_doc()),
				);
			});
		}
		if (served) {
			frm.add_custom_button(__('Stop'), () => {
				frappe.confirm(
					__('Stop this engine? It stops serving and stays on the box until you start it again.'),
					() => frm.call('stop').then(() => frm.reload_doc()),
				);
			});
		}
		if (status === 'Inactive') {
			frm.add_custom_button(__('Start'), () => {
				frm.call('start').then(() => frm.reload_doc());
			});
		}
		// Not while Provisioning: it would race the play.
		if (served || status === 'Inactive') {
			frm.add_custom_button(__('Tear Down'), () => {
				frappe.confirm(
					__('Stop and remove this instance from its box? The model stops serving here, and this deployment cannot be redeployed — make a new one.'),
					() => frm.call('teardown').then(() => frm.reload_doc()),
				);
			}).addClass('btn-danger');
		}
	},
});

function setup_log_view(frm) {
	frm.log_lines = frm.log_lines || [];
	set_log_button(frm, frm.log_streaming);

	frappe.realtime.off('grove_log');
	frappe.realtime.on('grove_log', ({ lines, done }) => {
		// Batches the job publishes after Stop would land on the fetch's pane.
		if (frm.log_streaming) {
			frm.log_lines = frm.log_lines.concat(lines || []).slice(-LOG_LINE_LIMIT);
			render_logs(frm);
		}
		if (done) set_log_button(frm, false);
	});
}

function stop_log_stream(frm) {
	if (!frm.log_streaming) return;
	frm.call('stop_engine_logs');
	set_log_button(frm, false);
}

function clear_logs(frm) {
	frm.log_lines = [];
	render_logs(frm, true);
}

function render_logs(frm, force_scroll) {
	const $wrapper = frm.get_field('engine_logs').$wrapper;
	if (!$wrapper.find('pre').length) {
		$wrapper.html(
			$('<pre>').css({
				height: '60vh', overflow: 'auto', margin: 0, padding: '8px',
				background: 'var(--fg-color)', border: '1px solid var(--border-color)',
				'border-radius': 'var(--border-radius)', 'font-size': '11px',
				'white-space': 'pre-wrap',
			}),
		);
	}
	const pre = $wrapper.find('pre')[0];
	const follow = force_scroll || pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
	pre.textContent = frm.log_lines.join('\n');
	if (follow) pre.scrollTop = pre.scrollHeight;
}

function set_log_button(frm, streaming) {
	frm.log_streaming = streaming;
	set_log_heartbeat(frm, streaming);
	frm.get_field('toggle_log_stream').set_label(
		streaming ? __('Stop Streaming') : __('Start Streaming'),
	);
}

function set_log_heartbeat(frm, streaming) {
	clearInterval(frm.log_heartbeat);
	if (!streaming) return;
	frm.log_heartbeat = setInterval(() => {
		const route = frappe.get_route();
		const showing = route[0] === 'Form' && route[1] === frm.doctype && route[2] === frm.docname;
		if (!showing || !frm.log_streaming) {
			clearInterval(frm.log_heartbeat);
			return;
		}
		frappe.xcall('grove.log_relay.keep_streaming', {
			doctype: frm.doctype, docname: frm.docname,
		});
	}, LOG_PING_INTERVAL);
}
