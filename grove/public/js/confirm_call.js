frappe.provide("grove");

grove.confirm_call = function (frm, message, method, args) {
	frappe.confirm(message, () => frm.call(method, args).then(() => frm.reload_doc()));
};
