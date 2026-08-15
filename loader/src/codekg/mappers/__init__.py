"""Per-extractor mappers onto the canonical schema.

Adding an extractor means adding a module here that exposes the same five
functions - load, provenance, files, symbols, calls, external_refs - and
registering it in cli.MAPPERS. Nothing else in the loader changes. That is the
abstraction that keeps the normalization tax from scaling with tool count.
"""
