"""Every record from before `total_tokens` is summed off its own `usage` JSON. A record with no
entries stays 0."""

import frappe

from grove.billing.doctype.usage_record.usage_record import tokens_expression, usage_table


def execute():
	frappe.db.sql(
		f"""update `tabUsage Record` t
		join (select r.name, sum({tokens_expression()}) as tokens
			from `tabUsage Record` r, {usage_table()} group by r.name) s on s.name = t.name
		set t.total_tokens = s.tokens"""
	)
