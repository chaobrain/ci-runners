# Synthetic pilot programs

These programs implement [protocol 1](storage-pilot-protocol.md) only. They do
not enable an artifact backend, operate on real CI repository identities,
install keys/accounts/units, configure SSH, or perform TTL/deletion. Production
activation follows the separate [P1 deployment review](storage-p1-deployment.md).

## Programs and configuration

The three `bin/ci-storage-*` wrappers use an isolated root-managed Python 3.11+
environment at `/opt/ci-storage-pilot/venv` with `-I`. Install the reviewed wheel
into that dedicated environment, never into a runner environment or the system
Python. Install wrappers root-owned under the approved executable paths.
OpenSSH `ssh` and `ssh-keygen` are runtime dependencies; no Python dependency is
added. The store requires Linux file locks, no-follow opens and directory fsync.

All config files are absolute, root-owned regular files with no group/other
write permission and no links or writable, unprotected ancestors. `schema` is
integer 1 and `uid` is the positive OS uid of the role that runs the program;
the program refuses a different uid, including root. Private keys and role
work directories must belong to the corresponding uid, mode 0600/0700.

- Gateway config additionally has exactly `root`, `authorized_keys`,
  `allowed_signers`. The root contains prepared `staging`, `objects`, `ledger`
  directories private to the storage uid. The authorization file holds one
  plain Ed25519 key, with exactly restrict/from/command options; no pty or
  forwarding relaxation. Its fingerprint is the authenticated peer binding.
  Root-managed SSH policy must also disable other authentication/key sources.
- Issuer config additionally has exactly `key`, `client`, `audit_root`.
  `client` is the approved SSH public-key SHA256 fingerprint. A canonical grant
  is read from a private file; the issuer validates its scope before signing,
  writes a new exclusive/fsynced jti audit record and emits the request header.
  It does not derive GitHub permissions from caller environment variables.
- Probe config additionally has exactly `host`, `user`, `key`, `known_hosts`,
  `client`. The SSH host key file is a root-managed trust file. The client
  ignores global/user SSH config and agents, pins host-key checking, disables
  forwarding and interactive authentication, and uses only its own key.

The issuer and gateway never share the issuer private key. Only an administrator
may deliver reviewed synthetic request headers to the isolated probe identity.
These files and real endpoint/account/path bindings stay outside the repository.

Actual entrypoint syntax after approved installation:

```text
ci-storage-issuer --config <issuer-config> --grant <reviewed-canonical-grant>
ci-storage-gateway --config <gateway-config>
ci-storage-probe --config <probe-config> --request <issued-request> --output <new-private-directory>
ci-storage-probe --config <probe-config> --request <publish-request> --manifest <manifest> --payload <payload> --output <new-private-directory>
```

The gateway is a forced stdio command, not a listening daemon. It refuses any
SSH original command other than `ci-storage-pilot-v1`, authenticates before
body reads, acquires a cross-process transfer slot and durably admits the jti
before reading the body. Truncated authenticated requests therefore consume a
retry. It rechecks expiry after waiting and before releasing data. Root policy,
the single-key SSH restriction and aggregate cgroup limits still need target-host
acceptance; local stdio tests are not SSH authentication tests.

## Object and failure behavior

Object names are hashes of validated synthetic identities, never client paths.
Publishing is serialized with quota admission, private staging, file/directory
fsync and immutable directory rename. An identical retry returns the original
reference; conflicting or damaged objects fail. A crash after commit cannot
make an earlier attempt substitute for a missing current one. List reads only
its signed run-attempt scope and refuses a corrupt/incomplete listing.

The 1 GiB object budget counts staged and completed allocated bytes; 50 GiB
free-space floor is enforced before writes. This is application admission,
not a filesystem quota. The durable replay ledger has a 4096-grant cap, three
uses per jti, no automatic pruning. Failed or abandoned staging is never
readable as a completed object. Four lock slots cover body and response transfer.

Probe transport retries share one 300-second deadline, at most three attempts.
Authorization, format, integrity, conflict, replay, missing data and quota errors
do not fall back or retry. Local output is written only to a newly created
private directory; its completion record is last, and failed local writes remove
only this attempt's files. An existing result directory is never overwritten.

## Review and tests

`tests/test_storage_pilot.py` covers actual OpenSSH signatures, namespace/signer/
peer mismatch, exact synthetic scopes, expiry, booleans, immutable publication,
lost-response recovery, staging/capacity, bounded replay ledger, cross-process
slots, scoped listing, protected paths, bounded framing and local result cleanup.
Subprocess stdio tests transfer a 64 MiB object, but deliberately bypass SSH
admission and target-host resource controls. The normal mutation runner registers
30 pilot mutants, each with the specific test that kills it. Verification scratch
uses a checked root-owned temporary namespace, ignoring inherited TMPDIR/TEMP.

No release version or major tag is moved. The source and wheel digests, final
private config diffs, host-key fingerprints, root-owned environment, new role
uids, approved owners and deployment approval must be recorded before activation.
Real CI job issuance and isolation, part/plan/report integration, cross-run maps
and cleanup remain outside this synthetic pilot implementation.
