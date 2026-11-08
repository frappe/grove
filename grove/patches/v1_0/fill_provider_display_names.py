"""Model Provider has a Display Name now: the vendors the Select knows get theirs."""

import frappe

LABELS = {
	"anthropic": "Anthropic",
	"baseten": "Baseten",
	"deepseek": "DeepSeek",
	"frappe": "Frappe",
	"morph": "Morph",
	"openai": "OpenAI",
}


def execute():
	for provider_name, display_name in LABELS.items():
		frappe.db.set_value(
			"Model Provider",
			{"provider_name": provider_name},
			"display_name",
			display_name,
			update_modified=False,
		)
