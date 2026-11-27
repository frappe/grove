frappe.provide("grove");

grove.confirm_call = function (frm, message, method, args) {
	frappe.confirm(message, () => frm.call(method, args).then(() => frm.reload_doc()));
};

// Start or end maintenance on a pathway box. Starting offers to pause the every-minute push to
// every box; ending offers to resume it when it is paused.
grove.toggle_maintenance = async function (frm, start_message) {
	const start = !frm.doc.is_in_maintenance;
	const paused = await frappe.db.get_single_value("Grove Settings", "pause_projection");
	const message = start ? start_message : __("{0} serves new requests again.", [frm.doc.name]);
	const fields = [{ fieldtype: "HTML", options: `<p>${message}</p>` }];
	if (start && !paused) {
		fields.push({
			fieldname: "pause_projection",
			fieldtype: "Check",
			label: __("Pause Projection"),
			description: __(
				"Also stop the every-minute push to every box. Key mints and cap changes are refused while it is paused."
			),
		});
	}
	if (!start && paused) {
		fields.push({
			fieldname: "resume_projection",
			fieldtype: "Check",
			label: __("Resume Projection"),
			default: 1,
			description: __(
				"Projection is paused. Start the every-minute push to every box again."
			),
		});
	}
	frappe.prompt(
		fields,
		(values) => {
			const args = { on: start ? 1 : 0 };
			if (values.pause_projection) args.pause_projection = 1;
			if (values.resume_projection) args.pause_projection = 0;
			frm.call("set_maintenance", args).then(() => frm.reload_doc());
		},
		start ? __("Start Maintenance") : __("End Maintenance"),
		start ? __("Start") : __("End")
	);
};
