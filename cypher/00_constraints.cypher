// Canonical schema constraints and indexes.
// Idempotent - safe to re-run on every load.

CREATE CONSTRAINT repo_id IF NOT EXISTS
FOR (n:Repo) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT file_id IF NOT EXISTS
FOR (n:File) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT symbol_id IF NOT EXISTS
FOR (n:Symbol) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT package_id IF NOT EXISTS
FOR (n:Package) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT external_ref_id IF NOT EXISTS
FOR (n:ExternalRef) REQUIRE n.id IS UNIQUE;

// Cross-repo resolution matches on qualified name within a candidate repo,
// so this index carries the linking pass.
CREATE INDEX symbol_qname IF NOT EXISTS
FOR (n:Symbol) ON (n.qname);

CREATE INDEX symbol_repo IF NOT EXISTS
FOR (n:Symbol) ON (n.repo);

CREATE INDEX symbol_name IF NOT EXISTS
FOR (n:Symbol) ON (n.name);

CREATE INDEX external_ref_module IF NOT EXISTS
FOR (n:ExternalRef) ON (n.module);

// The `python-module-path` linking rule joins ExternalRef.module against the
// file that provides that module path. Without this index that step is a label
// scan of every File per unresolved import.
CREATE INDEX file_module IF NOT EXISTS
FOR (n:File) ON (n.module);

CREATE INDEX file_repo IF NOT EXISTS
FOR (n:File) ON (n.repo);

// Vulnerability layer. `id` is OSV's (usually a GHSA); `cve` is the alias people
// actually search for, and several OSV records can share one CVE.
CREATE CONSTRAINT vulnerability_id IF NOT EXISTS
FOR (n:Vulnerability) REQUIRE n.id IS UNIQUE;

CREATE INDEX vulnerability_cve IF NOT EXISTS
FOR (n:Vulnerability) ON (n.cve);

// Q18/Q19 join advisories to import sites through ExternalRef.root_module.
CREATE INDEX external_ref_root_module IF NOT EXISTS
FOR (n:ExternalRef) ON (n.root_module);

CREATE INDEX package_name IF NOT EXISTS
FOR (n:Package) ON (n.name);

// Free-text search over symbol names, used by the retrieval comparison arm.
CREATE FULLTEXT INDEX symbol_fulltext IF NOT EXISTS
FOR (n:Symbol) ON EACH [n.name, n.qname, n.docstring];
