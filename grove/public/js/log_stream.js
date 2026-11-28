frappe.provide("grove");

grove.LOG_LINE_LIMIT = 2000;
// Well inside the server's 45s liveness TTL, so one slow ping doesn't cut the stream.
grove.LOG_PING_INTERVAL = 15000;

// Shows the `grove_log` stream in the `field` HTML field; the form's `toggle_log_stream` Button
// field starts and stops it.
grove.setup_log_view = function (frm, field) {
	frm.log_lines = frm.log_lines || [];
	grove.render_logs(frm, field);
	grove.set_log_streaming(frm, frm.log_streaming);

	frappe.realtime.off("grove_log");
	frappe.realtime.on("grove_log", ({ lines, done }) => {
		// The job takes a moment to notice Stop: drop what it publishes in the meantime.
		if (frm.log_streaming) {
			frm.log_lines = frm.log_lines.concat(lines || []).slice(-grove.LOG_LINE_LIMIT);
			grove.render_logs(frm, field);
		}
		if (done) grove.set_log_streaming(frm, false);
	});
};

grove.start_log_stream = async function (frm, method) {
	await frm.call(method);
	grove.set_log_streaming(frm, true);
};

grove.stop_log_stream = function (frm, method) {
	if (!frm.log_streaming) return;
	frm.call(method);
	grove.set_log_streaming(frm, false);
};

grove.show_logs = function (frm, field, lines) {
	frm.log_lines = lines;
	grove.render_logs(frm, field, true);
};

grove.render_logs = function (frm, field, force_scroll) {
	const $wrapper = frm.get_field(field).$wrapper;
	if (!$wrapper.find("pre").length) {
		$wrapper.html(
			$("<pre>").css({
				height: "60vh",
				overflow: "auto",
				margin: 0,
				padding: "8px",
				background: "var(--fg-color)",
				border: "1px solid var(--border-color)",
				"border-radius": "var(--border-radius)",
				"font-size": "11px",
				"white-space": "pre-wrap",
			})
		);
	}
	const pre = $wrapper.find("pre")[0];
	const follow = force_scroll || pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
	pre.textContent = frm.log_lines.join("\n");
	if (follow) pre.scrollTop = pre.scrollHeight;
};

grove.set_log_streaming = function (frm, streaming) {
	frm.log_streaming = streaming;
	clearInterval(frm.log_heartbeat);
	if (streaming) {
		frm.log_heartbeat = setInterval(
			() => grove.keep_log_streaming(frm),
			grove.LOG_PING_INTERVAL
		);
	}
	frm.get_field("toggle_log_stream").set_label(
		streaming ? __("Stop Streaming") : __("Start Streaming")
	);
};

grove.keep_log_streaming = function (frm) {
	const [view, doctype, docname] = frappe.get_route();
	const showing = view === "Form" && doctype === frm.doctype && docname === frm.docname;
	if (!showing || !frm.log_streaming) {
		clearInterval(frm.log_heartbeat);
		return;
	}
	frappe.xcall("grove.log_relay.keep_streaming", {
		doctype: frm.doctype,
		docname: frm.docname,
	});
};
