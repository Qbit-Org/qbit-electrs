# Repository rulesets

These files are this repository's GitHub rulesets, as they are live. GitHub
doesn't read them. After a pull request that changes one of them merges, a
repository admin applies it with `gh` from the repository root.
[`../repository-settings.json`](../repository-settings.json) holds the
merge-method setting in the same way.

| File | Enforces |
| --- | --- |
| `signed-commits.json` | A verified signature on every commit added to `main`. No bypass actors. |
| `required-checks.json` | `main`: the `signed-landing` check must pass. No bypass actors. |
| `public-main-minimal.json` | `main`: pull requests with one approval and resolved review threads, merge commits only; no deletion or force push. Organization admins can bypass it when merging a pull request. |

Every commit that a push or merge adds to `main` needs a verified signature.
Commits already on the branch aren't checked again, but an old unsigned commit
that a push or merge brings into an existing branch from another one is
refused like a new one. Commits on a pull request's branch don't need one
while it is open. Before it merges, a maintainer rewrites the branch as one
commit, signs that commit with their own key and pushes it, and then merges it
with a merge commit, which GitHub signs. So the target branch gains one commit
a person signed, and one merge commit. For a pull request from a fork, that
push needs "Allow edits by maintainers". Without it, a maintainer opens the
same change from a branch in this repository.

Bringing in upstream mempool/electrs history is not covered by this. Rewriting
it as one commit would drop upstream's lineage, so it has to land as it is,
and every commit in it then needs a verified signature. How that is done is
still to be settled; whatever the answer, its head branch has to be listed in
`AS_IS_HEADS` in `main`'s copy of the workflow before it can merge.

The maintainer's push dismisses earlier approvals, and whoever pushed can't
give the new one. Someone else approves again, or an organization admin merges
the pull request through the review ruleset's bypass. That bypass works only
for merging a pull request, and it covers the review rules only: nobody can
bypass the required checks. CI is not a required check here, so wait for it to
pass on the signed commit before merging.

[`../workflows/signed-landing.yml`](../workflows/signed-landing.yml) reports
the `signed-landing` check on every pull request. It passes when the pull
request is one commit that a person signed with an OpenPGP key that is on
their own GitHub account, and fails until then: a red `signed-landing` on an
open pull request usually means it hasn't been squashed and signed yet. If its
message says it could not check, do what the message says, which is usually to
re-run it. GitHub's signature rule can't tell that by itself, because it
accepts any verified signature, GitHub's own included, such as on a web edit
or an "Update branch" merge. The check is required by a ruleset with no bypass
actors, so the merge button works only once it is green. It always runs from
the default branch's copy of the workflow, `main`'s, whatever branch the pull
request targets, so a pull request can't change the rule it is judged by. Its
result belongs to the commit, so it passes only if every open pull request
that the commit heads may land, drafts aside and whatever branch each comes
from: a second pull request that takes the same commit into another branch,
where it is several commits, fails both until it is closed or turned into a
draft, either of which runs the check again, or is made ready too and the
check re-run. A run starts a few seconds after its event, and until then the
commit keeps the result it had. A change that a workflow makes with its own
token starts no run at all. It guards against landing a pull request by
mistake, not against someone set on getting round it: a pull request could
still add a workflow of its own that reports a check of the same name. Review
catches that where a branch requires review, and the maintainers audit merged
pull requests daily, from outside this repository, and report anything that
got through.

If the check stops reporting, no pull request can merge into a branch that
requires it, and that includes the pull request that fixes the workflow,
because it too is judged by `main`'s copy. GitHub blocks `pull_request_target`
workflows in public repositories unless an Actions policy allows them. If that
policy names this file by its path, renaming the file stops the check. To get
out, a repository admin disables `required-checks` on GitHub (Settings → Rules
→ Rulesets), merges the fix, and applies the files again. Each open pull
request then needs a push or an edit to be judged by the fixed copy. The same
way out serves when another required check can't run. A run that stays queued,
as can happen after an Actions incident, leaves the commit's last result in
place too: cancel it, and edit the pull request's description to start
another. The check reads GitHub with the workflow's token, and every run in
the repository draws on that token's hourly allowance of requests. A flood of
pull request events can use it up, and the check then fails for every pull
request until the hour is over.

The repository's settings allow merge commits only (Settings → General → Pull
Requests). GitHub's squash merge would make the commit itself and sign it with
GitHub's key, so no person's signature would reach the branch. Its rebase
merge makes commits with no signature, which the rule refuses.

## Apply a change

Define `repo` and `apply` first. This block changes nothing on GitHub:

```bash
repo=repos/Qbit-Org/qbit-electrs
apply() {
  local name id
  name=$(jq -er 'select(.rules and .conditions) | .name' "$1") || { echo "$1: not a ruleset file"; return 1; }
  id=$(gh api "$repo/rulesets?includes_parents=false&per_page=100" --jq ".[] | select(.name == \"$name\") | .id") || return
  if [ -n "$id" ]; then
    gh api -X PUT "$repo/rulesets/$id" --input "$1" --jq '"updated \(.name) (\(.id))"'
  else
    gh api -X POST "$repo/rulesets" --input "$1" --jq '"created \(.name) (\(.id))"'
  fi
}
```

Don't apply a file that requires `signed-landing` until `signed-landing.yml`
is on `main` and the Actions policy allows it: until then nothing reports the
check, and no pull request into a branch that requires it can merge.

Then run `apply` once for each ruleset file that changed. It updates the
ruleset of that name, or creates it when GitHub has none. For example:

```bash
apply .github/rulesets/public-main-minimal.json
```

A ruleset is found by its name. To rename one, rename it on GitHub first, then
apply the file. When a required check moves from one ruleset to another, apply
the one that gains it first.

`apply` is for ruleset files. Apply a change to `repository-settings.json`
with:

```bash
gh api -X PATCH "$repo" --input .github/repository-settings.json --silent
```

The merge-method setting isn't a ruleset. To set a repository up from these
files, apply `signed-commits.json` first, allow merge commits, apply the other
files, then turn squash and rebase merges off. No step leaves the repository
unable to merge, and unsigned commits can't land after the first one. The
chain stops at the first step that fails. Fix that step and run the chain
again: applying a file twice is harmless. Run it only once
`signed-landing.yml` is on `main`, the default branch, and the Actions policy
allows it. A pull request that is already open shows the check as expected
until its next push or edit: edit its title or description to start a run.

```bash
apply .github/rulesets/signed-commits.json &&
  gh api -X PATCH "$repo" -F allow_merge_commit=true --silent &&
  apply .github/rulesets/required-checks.json &&
  apply .github/rulesets/public-main-minimal.json &&
  gh api -X PATCH "$repo" --input .github/repository-settings.json --silent
```

## Check that GitHub matches

Run this as a repository admin. GitHub leaves the bypass actors out for anyone
else, and the check then says so instead of comparing.

```bash
repo=repos/Qbit-Org/qbit-electrs
norm='{name, target, enforcement, conditions, rules, bypass_actors} | walk(if type == "array" then sort else . end)'
if live=$(gh api "$repo/rulesets?includes_parents=false&per_page=100" --jq '.[] | "\(.id)\t\(.name)"'); then
  names=$(jq -r 'select(.rules and .conditions) | .name' .github/rulesets/*.json)
  for f in .github/rulesets/*.json; do
    name=$(jq -er 'select(.rules and .conditions) | .name' "$f") || { echo "$f: not a ruleset file"; continue; }
    id=$(awk -F '\t' -v name="$name" '$2 == name { print $1 }' <<<"$live")
    if [ -z "$id" ]; then
      echo "$name: not on GitHub"
    elif ! got=$(gh api "$repo/rulesets/$id"); then
      echo "$name: could not be read"
    elif ! jq -e '.bypass_actors != null' <<<"$got" >/dev/null; then
      echo "$name: GitHub left out the bypass actors, so run this as a repository admin"
    elif ! want=$(jq -S "$norm" "$f") || ! have=$(jq -S "$norm" <<<"$got"); then
      echo "$name: could not be compared"
    elif [ "$want" = "$have" ]; then
      echo "$name: matches"
    else
      diff <(echo "$want") <(echo "$have")
      echo "$name: differs (lines with < are the file's, > are GitHub's)"
    fi
  done
  while IFS=$'\t' read -r id name; do
    [ -z "$name" ] || grep -qxF "$name" <<<"$names" || echo "$name: on GitHub, but has no file"
  done <<<"$live"
  if ! want=$(jq -S . .github/repository-settings.json) || ! got=$(gh api "$repo") || ! have=$(jq -S '{allow_merge_commit, allow_rebase_merge, allow_squash_merge}' <<<"$got"); then
    echo "merge settings: could not be compared"
  elif [ "$want" = "$have" ]; then
    echo "merge settings: match"
  else
    diff <(echo "$want") <(echo "$have")
    echo "merge settings: differ (lines with < are the file's, > are GitHub's)"
  fi
else
  echo "could not list the rulesets on GitHub"
fi
```

Every line should say `matches` or `match`. `includes_parents=false` leaves
out organization rulesets, which these files don't hold.
