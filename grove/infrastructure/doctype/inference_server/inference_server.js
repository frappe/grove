frappe.ui.form.on("Inference Server", {
	refresh(frm) {
		show_front_warning(frm);
		if (frm.is_new()) return;
		render_gpus(frm);
		if (frm.doc.status === "Terminated") return;

		if (!frm.doc.is_provisioned) {
			frm.add_custom_button(__("Setup"), () =>
				grove.confirm_call(
					frm,
					__(
						"Provision {0} for serving? It installs Docker, the NVIDIA toolkit, exporters and its front — several minutes of Ansible.",
						[frm.doc.name]
					),
					"setup"
				)
			);
		}
		if (frm.doc.is_standalone) {
			grove.add_tls_buttons(
				frm,
				__("Push the Geography's certificate to {0} and reload nginx?", [frm.doc.name])
			);
		}
		frm.add_custom_button(__("Update Scrape Auth"), () =>
			grove.confirm_call(
				frm,
				__(
					"Rewrite {0}'s exporter password from Grove Settings? Scrapes with the old one fail from then on.",
					[frm.doc.name]
				),
				"update_scrape_auth"
			)
		);
		grove.add_archive_button(frm);
	},

	ingress: show_front_warning,

	is_standalone(frm) {
		// The Ingress field hides once this is ticked, and a hidden value would fail the save.
		if (frm.doc.is_standalone && frm.doc.ingress) frm.set_value("ingress", "");
		show_front_warning(frm);
	},
});

function show_front_warning(frm) {
	const has_no_front =
		frm.doc.status !== "Terminated" && !frm.doc.ingress && !frm.doc.is_standalone;
	const message = frm.doc.is_provisioned
		? __(
				"Set up with no Ingress Server and not Standalone — the gateways dial it by IP, and Standalone is fixed on a set-up box."
		  )
		: __(
				"No Ingress Server and not Standalone — Setup will refuse this box. Pick the ingress that fronts it, or tick Standalone to have the gateways dial it directly."
		  );
	frm.set_intro(has_no_front ? message : "", "orange");
}

function render_gpus(frm) {
	frm.call("get_gpu_allocation").then((r) => {
		grove.render_gpu_table(frm.fields_dict.gpus_html?.$wrapper, r.message || [], {
			empty: __("No GPUs on this server's Machine — run Scan GPUs on it."),
			note: __("this box's Machine, live"),
		});
	});
}
