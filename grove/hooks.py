app_name = "grove"
app_title = "Grove"
app_publisher = "developers@frappe.io"
app_description = "An Inference Platform"
app_email = "developers@frappe.io"
app_license = "mit"

# The GPU table, drawn the same way on the Machine that owns the cards and the Inference Server
# that serves from them; and the confirm every state-changing form button goes through.
app_include_js = ["/assets/grove/js/gpu_table.js", "/assets/grove/js/confirm_call.js"]

# The "open" filter behind every connections badge
notification_config = "grove.notifications.get_notification_config"

fixtures = [
	{"dt": "Role", "filters": [["name", "in", ["Grove Control", "Grove User"]]]},
	{"dt": "Model Provider", "filters": [["is_self_hosted", "=", 1]]},  # TODO: move to after migrate/after install
]

scheduler_events = {
	"cron": {
        # every minute
		"*/1 * * * *": [
            "grove.pathway.projection.sync_projection",
        ],
		"*/2 * * * *": [
			"grove.cloud_provider.reconcile.sync_all",
		],
		"*/5 * * * *": [
			# One RDB per Active store into the weights bucket, over SSH; off until the Mirror keys are set.
			"grove.grove.doctype.gateway_store.gateway_store.backup_all",
		],
	},
    "hourly_long": [
		# Drains every store; a drain is re-sent until acknowledged, so a failed pull loses nothing.
		"grove.pathway.usage.pull_all",
		# Only when certbot says it is due, and pushed only if the certificate changed.
		"grove.tls.renew_fleet_certificate",
		"grove.cloud_provider.schedule.run_due_pods",
	],
}

export_python_type_annotations = True
require_type_annotated_api_methods = True

default_log_clearing_doctypes = {
	"Pathway Sync": 60,
	"Pod Activity": 60,
	"Stuck Usage": 90,
}
