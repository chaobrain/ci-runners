# brainspike's host lock and slot scripts

Unmodified copies of brainspike's `scripts/host_lock.sh`, `scripts/host_lock.py` and
`scripts/ci_slot.sh` (Apache 2.0), taken from brainspike `main` at bc6bc95b on
2026-11-07. The interop tests (`tests/test_interop.py`) run `ci-lock` and `ci-slot`
against them (design decision 12.9, D5, §7; RUN-7 acceptance 2). `ci_slot.sh` looks for
`ci_runs.py` beside it and skips its newest-run registry without it, as here. Set
`CI_RUNNERS_INTEROP_SRC` to a brainspike `scripts/` directory to test a live checkout
instead.

| file | sha256 |
|---|---|
| host_lock.sh | 5e4b98f582f601bfedbcce94bc7db7582098e96827e2691a3495f1cefa411bee |
| host_lock.py | a7098d7cef0489ff9e1dc81eddb516e57e176c7bd8633872586ca75c7e661aeb |
| ci_slot.sh | 7d33373cc4325311dc6e7879cc98641fedfa2a5c33a4d72bb3415305de6dde1d |
