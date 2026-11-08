// The island entry for the Ops page. Desk imports this module and calls `mount`.
import { mountVueIsland } from "@framework/ui/island";

import Page from "./ops.vue";

// frappe-ui's request layer reads the CSRF token off `window.csrf_token`; desk keeps it on
// `frappe.csrf_token` alone, so a POST from inside the island would be refused without this.
if (!window.csrf_token && window.frappe?.csrf_token) {
	window.csrf_token = window.frappe.csrf_token;
}

export const mount = (el, context) => mountVueIsland(el, { ...context, component: Page });
