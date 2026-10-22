app_name = "grove"
app_title = "Grove"
app_publisher = "developers@frappe.io"
app_description = "An Inference Platform"
app_email = "developers@frappe.io"
app_license = "mit"

app_include_js = ["/assets/grove/js/gpu_table.js", "/assets/grove/js/confirm_call.js"]

notification_config = "grove.notifications.get_notification_config"

fixtures = [
	{"dt": "Role", "filters": [["name", "in", ["Grove Control"]]]},
]

after_install = "grove.catalog.seed.insert_missing"
after_migrate = ["grove.catalog.seed.insert_missing"]

scheduler_events = {
	"cron": {
		"*/1 * * * *": [
			"grove.pathway.projection.sync_projection",
		],
		"*/2 * * * *": [
			"grove.cloud_provider.reconcile.sync_all",
		],
		"0 */6 * * *": [
			"grove.pathway.key_stats.pull_all",
		],
		"*/5 * * * *": [
			"grove.grove.doctype.gateway_store.gateway_store.backup_all",
		],
	},
	"hourly": [
		"grove.grove.doctype.grove_api_key.grove_api_key.revoke_expired",
	],
	"hourly_long": [
		"grove.pathway.usage.pull_all",
		"grove.tls.renew_fleet_certificate",
		"grove.cloud_provider.schedule.run_due_pods",
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
