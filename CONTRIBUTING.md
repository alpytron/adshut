# Contributing to AdShut

Thank you for helping. Issues and pull requests are welcome.

## Developer Certificate of Origin

AdShut uses the [Developer Certificate of Origin 1.1](https://developercertificate.org/)
(DCO) instead of a contributor licence agreement. By adding a `Signed-off-by` line to a commit
you certify that you wrote the change, or otherwise have the right to submit it under the
project's licences (GPL-3.0-or-later; AGPL-3.0-or-later for the template server).

Sign off every commit with your real name:

```sh
git commit -s -m "Describe the change"
# adds: Signed-off-by: Your Name <you@example.com>
```

Forgot? `git commit --amend -s` for the last commit, or `git rebase --signoff main` for a branch.

## Ground rules

- **Privacy**: no change may send camera pictures or audio off the device the camera is
  attached to, or collect data outside the template server protocol.
- **No content**: never commit recordings, fingerprints, templates, or screenshots of
  broadcasts. Test fixtures are synthetic (`python/tests/make_fixtures.py`).
- **Two implementations**: the live loop exists in Java (`android/src/adshut/core`) and Python
  (`python/adshut`). Change them together and keep `./tests.sh` passing.
- New source files start with an SPDX line (`GPL-3.0-or-later`, or `AGPL-3.0-or-later` for the
  template server) and the copyright line.
