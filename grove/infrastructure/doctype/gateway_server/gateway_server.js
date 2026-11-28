frappe.ui.form.on("Gateway Server", {
	refresh(frm) {
		if (frm.is_new()) return;
		add_setup_buttons(frm);
		if (frm.doc.status === "Active") grove.add_vector_button(frm);
		add_danger_buttons(frm);
		if (frm.doc.status === "Active" && frm.doc.admin_url) add_live_buttons(frm);
		if (frm.doc.is_in_maintenance) {
			frm.set_intro(__("In maintenance — new requests get 503."), "orange");
		}
	},
});

function add_setup_buttons(frm) {
	if (["Pending", "Broken"].includes(frm.doc.status)) {
		frm.add_custom_button(
			__("Provision"),
			() =>
				grove.confirm_call(
					frm,
					__(
						"Run the full gateway play on {0}? It installs the agent and its config, and starts it.",
						[frm.doc.name]
					),
					"setup"
				),
			__("Gateway")
		);
	}
	if (frm.doc.admin_url) {
		frm.add_custom_button(__("Ping"), () => frm.call("ping"), __("Gateway"));
	}
	if (frm.doc.machine) {
		grove.add_agent_buttons(frm, __("Gateway"));
		grove.add_tls_buttons(frm);
	}
}

function add_danger_buttons(frm) {
	if (frm.doc.status === "Terminated") return;
	frm.add_custom_button(__("Resize Server"), () => grove.prompt_resize(frm), __("Danger"));
	grove.add_archive_button(frm);
}

function add_live_buttons(frm) {
	frm.add_custom_button(
		__("Check State"),
		() => {
			frm.call("check_state");
		},
		__("Gateway")
	);
	frm.add_custom_button(
		__("Full Sync"),
		() =>
			grove.confirm_call(
				frm,
				__(
					"Push every key, user, group and route to {0}'s store, whether or not it already holds them?",
					[frm.doc.name]
				),
				"full_sync"
			),
		__("Gateway")
	);
	frm.add_custom_button(
		__("Pull Usage"),
		() =>
			grove.confirm_call(
				frm,
				__("Drain {0}'s store now and bill what it holds?", [frm.doc.name]),
				"pull_usage"
			),
		__("Gateway")
	);
	grove.add_maintenance_button(
		frm,
		__("New requests to {0} get 503 until maintenance ends; running ones finish.", [
			frm.doc.name,
		]),
		__("Gateway")
	);
	add_cache_button(frm);
}

function add_cache_button(frm) {
	if (frm.doc.isolate_caches) {
		frm.add_custom_button(
			__("Share Caches"),
			() =>
				grove.confirm_call(
					frm,
					__("Teams share one prompt cache on {0} again.", [frm.doc.name]),
					"set_isolate_caches",
					{ on: 0 }
				),
			__("Gateway")
		);
	} else {
		frm.add_custom_button(
			__("Isolate Caches"),
			() =>
				grove.confirm_call(
					frm,
					__(
						"Every team gets its own prompt-cache partition on {0}: cache_salt on an engine, prompt_cache_key at OpenAI, user_id at DeepSeek. No cache hits across teams.",
						[frm.doc.name]
					),
					"set_isolate_caches",
					{ on: 1 }
				),
			__("Gateway")
		);
	}
}
