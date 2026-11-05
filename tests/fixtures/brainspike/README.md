# brainspike's host lock and slot scripts

Unmodified copies of brainspike's `scripts/host_lock.sh` and `scripts/ci_slot.sh`
(Apache 2.0), taken from brainspike `main` at 0e2be7ff on 2026-11-05. The interop tests
(`tests/test_interop.py`) run `ci-lock` and `ci-slot` against them (design §7, D5;
RUN-1 acceptance 2). Set `CI_RUNNERS_INTEROP_SRC` to a brainspike `scripts/` directory
to test a live checkout instead.

| file | sha256 |
|---|---|
| host_lock.sh | 5e4b98f582f601bfedbcce94bc7db7582098e96827e2691a3495f1cefa411bee |
| ci_slot.sh | b87b772ec88a6b276eb126d269810f878c120ba21f986c61eb27ae9654df75dd |
