# Synthetic storage pilot protocol 1

Implementation contract for the P1 scheme requested on 2026-11-24. This freezes
the wire and grant format for isolated local tests; it does not approve host
deployment or admit real CI jobs. Deployment gates remain in
[storage-p1-deployment.md](storage-p1-deployment.md).

## Scope

Only repository pairs `(910001, pilot-a/probe)` and `(910002, pilot-b/probe)`,
run ids 920001/920002, run attempts 1/2, part id `probe`, part attempts 1/2
and commit SHA of forty zeroes are accepted. These are synthetic identities,
not lookups of or entitlements to real GitHub repositories. Objects are opaque
probe streams, never outcomes that a verdict may read. Standard part and plan
formats are not enabled by this protocol.

## Frame

One frame per SSH invocation: a 20-byte big-endian prefix `>4sIIQ` carries magic
`CISP`, header byte count, manifest byte count and payload byte count, followed
by those three byte strings and EOF. Header: 1..65536 bytes, manifest:
0..1048576 bytes, payload: 0..69206016 bytes. Check all lengths before buffering.
Reject truncation, extra frames, trailing bytes, non-UTF-8 JSON, duplicate keys,
non-finite numbers, unknown fields and unsupported versions. No paths or shell
arguments are transported. JSON canonicalization is UTF-8, sorted keys, ASCII
escapes, separators `,` and `:`, no NaN; signed body bytes must be canonical.

Request header fields are exactly `schema` (integer 1), `operation`
(`publish`, `read`, `list`), `grant` and `signature`. The last two are canonical
base64 strings of the signed body bytes and SSH armored signature, each at
most 16384 decoded bytes. Request operation must match the signed operation.
Only publish carries a manifest and payload; read/list carry neither.

Success response header has `schema:1`, `status:ok`, `result`; a read also
carries its manifest and payload, publish/list carry no body. Error header
has `schema:1`, `status:error`, `code` and no body. Codes are stable categories,
not credentials, file paths or raw exception messages. Client checks response
version, operation-specific result, identity, digests and bytes, and refuses
unexpected bodies. Nonzero SSH/program status is a transport failure.

## Signed grant

Fields are exactly `schema`, `audience`, `key_id`, `jti`, `nbf`, `exp`,
`operation`, `client`, `identity`, `manifest_sha256`, `payload_sha256`, `size`.
Schema is integer 1, audience `ci-storage-pilot-v1`, key id `pilot-issuer-v1`.
Jti is 32 lowercase hex characters. Times are positive integer UTC Unix
seconds, expiry after not-before and at most 600 seconds later. Allow not-before
clock skew of at most 30 seconds; expiry is exclusive with no grace. Client is
the SHA256 OpenSSH public-key fingerprint pinned in protected server policy.
SSH only admits that one plain Ed25519 probe key to this new login identity;
the grant must not supply its own authenticated peer identity.

Publish/read identity has exactly the seven fields of `bundles.Identity`.
List identity has only repository id/name, run id/attempt and SHA; results are
restricted to that exact run-attempt scope. Publish/read bind lowercase hex
SHA-256 manifest/payload digests and a positive payload size at most 66 MiB.
List uses empty digest strings and size zero. Booleans are never integers.

Use a distinct Ed25519 signing key, OpenSSH signature namespace
`ci-storage-capability-v1`, signer identity `ci-storage-p1-issuer`, and a
protected allowed-signers file. Bind key id, signer and namespace; verify
before accepting a body or accessing storage. The issuer accepts only reviewed
synthetic scopes, validates every field before signing, and never grants real
repository access. No issuer key is present on the probe client or gateway.

## Probe manifest and storage

Manifest fields are exactly `schema:1`, `kind:probe`, `status:complete`,
`identity`, `size`, `sha256`. Its exact bytes, identity, actual payload byte
count and digest must match the grant. Manifest digest pins those exact bytes.

Immutable object key is SHA-256 of the canonical full identity. A private
staging directory receives payload and manifest, both fsynced before directory
rename into the completed namespace; fsync the destination parent. An object
is visible only with its completed manifest and intact payload. Same identity
and exact manifest digest is idempotent; different bytes conflict. Crash after
rename but before response permits an identical retry, never overwrite.

Under a cross-process lock, durable jti accounting binds each jti to the
canonical grant digest and permits at most three authenticated invocations,
including failures after admission. Changing a jti's scope is denied. Keep at
most 4096 grant records, fail closed when full, no TTL or deletion in P1.
List is bounded by the synthetic identity space (16 objects), never partial.

Serialize store mutations and admission under a bounded lock, count completed
objects and staging occupancy toward the 1 GiB application budget, and refuse
new writes below 50 GiB free disk. Four cross-process admission slots bound
active transfers; locks/reads/writes/signature subprocesses share a 300-second
total deadline with at most 45 seconds of idle I/O. A client retries at most
three times within that same deadline, only for transport/busy errors. Grant,
integrity, quota, conflict, framing and missing-object errors are terminal.

## Execution boundary

Production entrypoints require Linux, protected absolute config/code/key paths,
separate storage/issuer/probe OS identities, pinned SSH host keys, and reviewed
aggregate resource controls. Gateway accepts only SSH original command
`ci-storage-pilot-v1`; rejects shell/scp/sftp requests. Neither configuration
nor trusted peer identity is accepted from a checkout or request. Source-level
local subprocess tests simulate this boundary, not actual SSH admission.
No command installs accounts, authorized keys, services, changes SSH settings,
deletes data or enables a CI backend. Host activation is a separate review.
