app_name = "grove"
app_title = "Grove"
app_publisher = "developers@frappe.io"
app_description = "An Inference Platform"
app_email = "developers@frappe.io"
app_license = "mit"

app_include_js = [
	"/assets/grove/js/gpu_table.js",
	"/assets/grove/js/confirm_call.js",
	"/assets/grove/js/resize_server.js",
]

notification_config = "grove.notifications.get_notification_config"

auth_hooks = ["grove.auth.hook"]

fixtures = [
	{"dt": "Role", "filters": [["name", "in", ["Grove Control"]]]},
]

# The seed writes Grove Settings first, so the fields after it get no default of their own.
after_install = ["grove.catalog.seed.insert_missing", "grove.patches.v1_0.set_report_resend_days.execute"]
after_migrate = ["grove.catalog.seed.insert_missing"]

scheduler_events = {
	"cron": {
		"*/1 * * * *": [
			"grove.pathway.projection.sync_projection",
			"grove.infrastructure.datum.push_roster",
		],
		"0 */6 * * *": [
			"grove.pathway.key_stats.pull_all",
		],
		"*/5 * * * *": [
			"grove.infrastructure.doctype.gateway_store.gateway_store.backup_all",
			"grove.infrastructure.cloud_provider.reconcile.sync_all",
		],
	},
	"hourly": [
		"grove.grove.doctype.grove_api_key.grove_api_key.revoke_expired",
		"grove.infrastructure.datum.refresh_tokens",
	],
	"hourly_long": [
		"grove.pathway.usage.pull_all",
		"grove.billing.usage_report.report_usage",
		"grove.infrastructure.tls.renew_fleet_certificate",
		"grove.infrastructure.cloud_provider.schedule.run_due_pods",
	],
}

export_python_type_annotations = True
require_type_annotated_api_methods = True

default_log_clearing_doctypes = {
	"Pathway Sync": 60,
	"Pod Activity": 60,
	"Provider Key Stats": 60,
	"Stuck Usage": 90,
	"Usage Record": 90,
}
