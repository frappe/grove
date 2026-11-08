def get_data():
	"""Connections: the gateways running on this store, every Ansible Play against its box and
	every Server Resize of it."""
	return {
		"fieldname": "server",
		"non_standard_fieldnames": {"Gateway Server": "gateway_store"},
		"dynamic_links": {"server": ["Gateway Store", "server_type"]},
		"transactions": [
			{"label": "Gateways", "items": ["Gateway Server"]},
			{"label": "Automation", "items": ["Ansible Play", "Server Resize"]},
		],
	}
