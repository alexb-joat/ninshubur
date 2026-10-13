"""Ninshubur: a policy-first intelligence crawler.

Three rules shape every module:
1. Only sources a human admitted after the trust checklist are ever fetched.
2. Only fields declared in a schema are ever stored; raw bodies never touch disk.
3. Malicious or malformed input quarantines the item, downgrades the source,
   holds affected suggestions and keeps serving from the last good snapshot.
"""

__version__ = "0.1.0"
