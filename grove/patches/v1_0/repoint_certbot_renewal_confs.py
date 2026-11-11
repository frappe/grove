"""A renamed site moved certbot's directory; its renewal confs still name the old one in every path,
which certbot refuses as broken. The live symlinks are relative and survive the move."""

import os
import re
from pathlib import Path

from grove import tls


def execute():
	root = os.path.dirname(tls.certbot_dir("config"))
	for path in Path(root, "config", "renewal").glob("*.conf"):
		conf = path.read_text()
		recorded = re.search(r"^archive_dir = (.+)/config/archive/", conf, re.MULTILINE)
		if recorded and recorded.group(1) != root:
			path.write_text(conf.replace(recorded.group(1), root))
