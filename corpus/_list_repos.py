"""Emit "<repo-id> <subpath>" per line for a corpus at or below a tier.

Shared by the Makefile's extract loop. Kept as a file rather than an inline
heredoc so quoting stays sane across make, bash, and python.

Usage: _list_repos.py <corpus.yaml> <corpus-id> <core|full>
"""

import sys

import yaml

config, corpus, tier = sys.argv[1], sys.argv[2], sys.argv[3]

doc = yaml.safe_load(open(config))
if corpus not in doc["corpora"]:
    sys.exit(f"no such corpus: {corpus} (have: {', '.join(doc['corpora'])})")

wanted = {"core"} if tier == "core" else {"core", "full"}
for repo in doc["corpora"][corpus]["repos"]:
    if repo.get("tier", "core") in wanted:
        print(repo["id"], repo.get("subpath", ""))
