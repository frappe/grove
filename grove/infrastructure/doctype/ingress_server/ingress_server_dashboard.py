def get_data():
	"""Ansible Play is a dynamic link, so server_type is pinned alongside server."""
	return {
		"fieldname": "server",
		"dynamic_links": {"server": ["Ingress Server", "server_type"]},
		"transactions": [{"label": "Automation", "items": ["Ansible Play"]}],
	}
