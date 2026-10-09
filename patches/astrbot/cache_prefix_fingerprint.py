# -*- coding: utf-8 -*-
"""Log hashes of outgoing message segments to locate changing cache prefixes."""
import ast
import pathlib
import sys


OLD_IMPORT = "import copy\nimport sys\n"
NEW_IMPORT = "import copy\nimport hashlib\nimport sys\n"
OLD_SITE = '''        if include_model:
            # For primary provider we keep explicit model selection if provided.
'''
NEW_SITE = '''        parts = []
        for msg in payload["contexts"]:
            role = msg.get("role") if isinstance(msg, dict) else msg.role
            raw = repr(msg) if isinstance(msg, dict) else msg.model_dump_json()
            digest = hashlib.blake2s(raw.encode("utf-8"), digest_size=6).hexdigest()
            parts.append("%s:%s" % (role, digest))
        logger.info("[mindscape_prefix] session=%s parts=%s",
                    self.req.session_id or "-", "|".join(parts))
        if include_model:
            # For primary provider we keep explicit model selection if provided.
'''


def patch_source(source):
    if NEW_SITE in source:
        return source
    if source.count(OLD_IMPORT) != 1 or source.count(OLD_SITE) != 1:
        raise ValueError("unsupported provider request layout")
    patched = source.replace(OLD_IMPORT, NEW_IMPORT).replace(OLD_SITE, NEW_SITE)
    ast.parse(patched)
    return patched


if __name__ == "__main__":
    source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
    pathlib.Path(sys.argv[2]).write_text(patch_source(source), encoding="utf-8")
