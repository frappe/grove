"""Usage Record's `total_tokens` is new: every record from before it is summed off its own `usage`
JSON, by the rule `tokens_expression` reads with. A record with no entries stays 0."""

import frappe

from grove.billing.doctype.usage_record.usage_record import tokens_expression, usage_table


def execute():
	frappe.db.sql(
		f"""update `tabUsage Record` t
		join (select r.name, sum({tokens_expression()}) as tokens
			from `tabUsage Record` r, {usage_table()} group by r.name) s on s.name = t.name
		set t.total_tokens = s.tokens"""
	)
