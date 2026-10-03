frappe.provide('grove');

// Every form button that changes a box or a record asks first; reads stay one click.
grove.confirm_call = function (frm, message, method, args) {
	frappe.confirm(message, () => frm.call(method, args).then(() => frm.reload_doc()));
};
