// Neo4j Enterprise Studio prerequisites - runs against the `system` database.
// https://neo4j.com/docs/enterprise-studio/current/prerequisites/
//
// Applied once by the `nes-init` service before Studio starts. Idempotent, so
// re-running after a `make nes` is harmless.
//
// Credentials here are local-dev only and are duplicated in docker-compose.yml
// under the enterprise-studio service. If you change one, change both - Studio
// fails to start with an opaque asset-store error if they drift.

// 1. Service account owning the tool asset database.
CREATE USER tools_service IF NOT EXISTS
  SET PASSWORD 'toolspassword'
  SET PASSWORD CHANGE NOT REQUIRED;

// `architect` carries the token and constraint privileges Studio needs to build
// its asset schema on first start.
GRANT ROLE architect TO tools_service;

// 2. The asset database must exist before Studio boots. Name must match
//    NES_assetStore_default_database in docker-compose.yml.
CREATE DATABASE `tools-storage` IF NOT EXISTS;

// 3. Privileges on the deployments users actually query. Without these, Bloom
//    silently shows no schema (it reads SHOW INDEXES / SHOW CONSTRAINTS) and
//    asset sharing cannot enumerate roles or users to share with.
GRANT SHOW CONSTRAINTS ON DATABASES * TO reader;
GRANT SHOW INDEXES ON DATABASES * TO reader;
GRANT SHOW ROLE ON DBMS TO reader;
GRANT SHOW USER ON DBMS TO reader;
