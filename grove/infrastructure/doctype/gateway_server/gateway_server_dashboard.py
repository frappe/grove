def get_data():
	"""Dynamic link, so server_type is pinned alongside server."""
	return {
		"fieldname": "server",
		"dynamic_links": {"server": ["Gateway Server", "server_type"]},
		"transactions": [{"label": "Automation", "items": ["Ansible Play", "Server Resize"]}],
	}
