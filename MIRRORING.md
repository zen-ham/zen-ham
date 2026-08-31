# Mirroring: gitlab.com/zenham → github.com/zen-ham

**TL;DR:** gitlab is primary. Every push to gitlab auto-syncs to github within ~30s. New gitlab repos get auto-provisioned on github within 24h (or on next manual run of the reconciler).

## Architecture

Two layers work together:

| Layer | What | Where | Trigger |
|---|---|---|---|
| **1. Push mirror** | Real-time per-repo `git push --mirror` from gitlab to github | Server-side on gitlab (no CI) | Every gitlab push |
| **2. Reconciler** | Provisions new repos, syncs visibility, adds missing mirrors | `assets/mirror_reconcile.py`, run by gitlab CI | Daily 06:00 UTC (schedule ID 4308638) + manual "web" trigger |

Layer 1 handles the frequent case (code pushes). Layer 2 handles the rare cases (new repos, private/public flips, mirror-config drift).

## Naming exceptions

Some gitlab paths don't map 1:1 to github names. Overrides in `NAME_MAP` inside `assets/mirror_reconcile.py`:

- `zenham` → `zen-ham` (github profile-readme repo uses hyphen because github username is `zen-ham`)

Add new mappings there when they come up.

## What auto-syncs

- Every branch push and force-push
- New branches
- New tags
- Branch deletions (because `keep_divergent_refs: false` in mirror config)
- Repo creation (via reconciler, next-day)
- Visibility flips (private ↔ public, via reconciler)

## What does NOT auto-sync

- Issues, MRs / PRs, wiki, labels, milestones
- CI configs, secret variables, environment settings
- Comments and reactions
- **Repo deletions** — deleting on gitlab does NOT delete on github (safety choice; recreate if reversed intentionally)
- Repo renames — rename on gitlab makes reconciler treat it as a new repo (creates github twin under new name; old github repo lingers)
- Fork/star relationships

## Tokens

| Token | Storage | Scopes needed | Consumed by |
|---|---|---|---|
| `WIDGETS_TOKEN` | gitlab CI/CD variable (masked+hidden) | `api`, `read_repository`, `write_repository` | `assets/make_widgets.py`, `assets/mirror_reconcile.py` (for the `GITLAB_TOKEN` env slot) |
| `GH_MIRROR_TOKEN` | gitlab CI/CD variable (masked+hidden) | `repo`, `delete_repo` on github | `assets/mirror_reconcile.py`; also embedded verbatim in every push-mirror URL |

Set both at: `gitlab.com/zenham/zenham/-/settings/ci_cd` → Variables → Add.

Flags to use: Protected off (schedules aren't from a protected branch necessarily), Masked on, Hidden on.

### Rotating GH_MIRROR_TOKEN

Because the github token is embedded in every push-mirror URL, rotating means updating N URLs. The reconciler does this for you now.

1. Create a new github PAT at `github.com/settings/tokens/new` with scopes `repo` + `delete_repo`
2. Update `GH_MIRROR_TOKEN` in gitlab CI/CD variables with the new value
3. Run the pipeline once **with `ROTATE_MIRRORS` set to `1`**: `gitlab.com/zenham/zenham/-/pipelines/new` → add variable `ROTATE_MIRRORS` = `1` → Run. The reconciler deletes and re-POSTs every github mirror so each URL carries the new token. Lines tagged `[mir~]` in the log are the rotated ones.
4. Revoke old token at `github.com/settings/tokens`

Notes:

- Step 3 matters even for repos that look healthy. Gitlab's remote-mirror API has **no way to update an existing mirror's `url`** (the `PUT` endpoint accepts `enabled`, `auth_method`, `keep_divergent_refs`, etc. but not `url`), so delete + re-create is the only route. A mirror that hasn't been pushed to since the rotation still reports `update_status: finished` while holding the dead token; it only fails on the next real push.
- Ordinary daily runs (no `ROTATE_MIRRORS`) self-heal any mirror whose last run failed with an auth error, so a missed rotation gets repaired within 24h of the first failing push.
- If the token is dead, the job now aborts immediately with a `FATAL: github auth failed` message naming this section, instead of crashing deeper in with a confusing `TypeError`.

## Excluding a repo from mirror

Add its gitlab `path` (not `path_with_namespace`) to `EXCLUDE` set in `assets/mirror_reconcile.py`.

Also delete the existing push mirror for that repo:
- UI: `gitlab.com/zenham/<repo>/-/settings/repository` → Mirroring repositories → trash icon
- API: `DELETE /projects/:pid/remote_mirrors/:mirror_id`

Otherwise pushes still mirror in real-time until the next reconcile.

## Adding a new mirror target (e.g. codeberg, sr.ht, bitbucket)

Push mirrors are additive — a gitlab repo can have N mirror targets simultaneously. Rough steps:

1. Copy `assets/mirror_reconcile.py` → `assets/mirror_reconcile_<provider>.py`. Replace github API calls with the new provider's API (repo create, visibility flip, repo lookup). Change the mirror URL template.
2. Add a new PAT env var for that provider (e.g. `CODEBERG_MIRROR_TOKEN`), register in gitlab CI/CD variables.
3. Add a new job to `.gitlab-ci.yml` that runs the new reconciler under the same schedule.
4. Update the target-name mapping (`NAME_MAP`) for any naming quirks on the new provider.
5. Update this doc: add row to the tokens table + note the new script in the Architecture table.

## Common maintenance

- **Check mirror health for one repo:**
  ```bash
  curl -H "PRIVATE-TOKEN: $GL" 'https://gitlab.com/api/v4/projects/:pid/remote_mirrors'
  ```
  Look for `last_error` (should be null) and `last_successful_update_at` (should be recent).

- **Manual force-sync one repo now:**
  ```bash
  git clone --mirror https://oauth2:$GL_TOKEN@gitlab.com/zenham/<repo>.git /tmp/<repo>
  git -C /tmp/<repo> push --mirror --force https://x-access-token:$GH_TOKEN@github.com/zen-ham/<repo>.git
  ```

- **Kick the reconciler now** (instead of waiting for 06:00 UTC):
  `gitlab.com/zenham/zenham/-/pipeline_schedules` → Play (schedule ID 4308638).

- **List all currently-mirrored repos:**
  ```bash
  for pid in ...; do curl -s -H "PRIVATE-TOKEN: $GL" "https://gitlab.com/api/v4/projects/$pid/remote_mirrors" | jq '.[] | {url, enabled, last_successful_update_at, last_error}'; done
  ```

## Files involved

| File | Repo | Purpose |
|---|---|---|
| `assets/mirror_reconcile.py` | `zenham/zenham` | The reconciler |
| `assets/make_widgets.py`    | `zenham/zenham` | Regens profile widgets (same schedule, different job) |
| `.gitlab-ci.yml`            | `zenham/zenham` | Defines `generate_widgets` + `mirror_reconcile` CI jobs |
| `MIRRORING.md`              | `zenham/zenham` | This file |

## Known quirks / gotchas

- **Push mirror not real-time on gitlab free tier?** Sometimes lags 1-5 min. If instant is needed, `git push` directly to both remotes.
- **Force-push on gitlab is force-mirrored to github.** This is the intended behavior but destroys any commits made directly to github. Don't commit directly to github; it's a mirror.
- **A gitlab repo made minutes before the reconciler run** gets provisioned that same run. A repo made minutes after has to wait ~24h. Kick the schedule manually if urgent.
- **Existing push-mirror URLs keep their old token forever** unless deleted + recreated. Rotation flow in the tokens section covers this (`ROTATE_MIRRORS=1`).
- **An expired github PAT breaks the whole rig silently.** Push mirrors just start failing server-side; nothing emails you about it. The daily reconciler is the tripwire: it warns when the PAT is within 14 days of expiry and fails loudly once it's dead. Classic PATs with an expiry date are the usual culprit; a no-expiry PAT or a fine-grained PAT with a long window avoids the repeat.
