"""No tracked file holds inventory (design §3.3, D1; RUN-1 acceptance 7).

``tests/deny.sha256`` holds salted hashes of the hosts' inventory tokens, regenerated
from a host's inventory by ``python -m ci_runners.inventory deny`` (``setup/plan.sh``
from RUN-1b). The lint also refuses IPv4 addresses, home-directory paths, GitHub
tokens and private keys, with or without the list. Run it before every push.
"""
from __future__ import annotations

import pathlib
import unittest

from ci_runners import inventory

ROOT = pathlib.Path(__file__).resolve().parents[1]
DENY = ROOT / 'tests' / 'deny.sha256'


class NoInventory(unittest.TestCase):
    def test_the_deny_list_is_not_empty(self):
        self.assertGreater(len(inventory.read_deny(DENY.read_text())), 10)

    def test_no_tracked_file_holds_inventory(self):
        findings = inventory.lint(str(ROOT), inventory.read_deny(DENY.read_text()))
        self.assertEqual([str(f) for f in findings], [])


if __name__ == '__main__':
    unittest.main()
