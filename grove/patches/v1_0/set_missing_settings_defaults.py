from grove.grove.doctype.grove_settings.grove_settings import set_missing_defaults

# Fields whose blank already acts as their default, so filling them changes nothing on a live site.
SAME_AS_BLANK_FIELDS = (
	"report_resend_days",
	"synthetic_session_ttl",
	"pause_projection",
	"pause_usage_pull",
	"pause_usage_report",
)


def execute():
	set_missing_defaults(SAME_AS_BLANK_FIELDS)
