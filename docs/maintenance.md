# Repository maintenance and releases

## Source layout

`custom_components/huawei_emma_management` is the complete HACS artifact. The reverse
Modbus/TLS implementation, certificate helper, and register catalog live there as the
canonical copies. The three Python files at repository root are compatibility launchers
for existing external-server installations; do not put new protocol logic in them.

Runtime secrets and generated files must never be committed: `.env`, virtual
environments, Python caches, coverage output, and PEM files are ignored. Before every
release, check `git status --ignored` and inspect the complete diff for tokens, private
keys, LAN addresses, device serial numbers, and captured customer data.

## Local validation

```bash
python -m pip install -r requirements.txt
python -m compileall -q custom_components huawei_modbus_server.py
python -m unittest -v
python -m json.tool hacs.json
python -m json.tool custom_components/huawei_emma_management/manifest.json
```

GitHub Actions runs the unit tests, HACS validation, and Home Assistant Hassfest on every
push and pull request and on a daily schedule. Do not merge or release while any of those
checks fail.

## Versioning and release sequence

Use semantic versions and keep the Git tag identical to `manifest.json`, prefixed with
`v` (for example integration `0.8.0`, tag `v0.8.0`).

1. Update `manifest.json` and release notes.
2. Run local validation and review the diff.
3. Merge to the default branch and wait for all checks.
4. Create a GitHub **Release**, not only a tag. HACS uses releases for its version list.
5. Install that release through HACS on a test Home Assistant instance.
6. Verify a clean install and an upgrade from the previous release in both embedded and
   external modes.

For HACS default-store submission, the public GitHub repository also needs a concise
description, enabled issues, relevant topics, a successful release, and passing HACS and
Hassfest actions. Suggested topics are `home-assistant`, `hacs`, `huawei`, `emma`,
`modbus`, `solar`, and `energy-management`.

## Dependency updates

Dependabot checks GitHub Actions and Python dependencies monthly. Keep `huawei-solar`
pinned until its register definitions and structured TOU encoding have passed hardware
tests. Test TLS negotiation, topology paging, grouped reads, scaled writes, schedule
round trips, and Home Assistant entity migration before changing that pin.

The manifest currently assumes the repository will be published as
`https://github.com/valtt/huawei-modbus-server` with `@valtt` as code owner. Change the
documentation, issue tracker, code owner, README HACS URL, and systemd documentation URL
together if the final GitHub owner or repository name differs.
