# Repository rulesets

These files are this repository's GitHub rulesets, as they are live. GitHub
doesn't read them. After a pull request that changes one of them merges, a
repository admin applies it with `gh` from the repository root.
[`../repository-settings.json`](../repository-settings.json) holds the
merge-method setting in the same way.

| File | Enforces |
| --- | --- |
| `signed-commits.json` | A verified signature on every commit of `main`. No bypass actors. |
| `public-main-minimal.json` | `main`: pull requests with one approval and resolved review threads, merge commits only; no deletion or force push. Organization admins can bypass it. |

Every commit that a push or merge adds to `main` needs a verified signature.
Commits already on the branch aren't checked again, but an old unsigned commit
that comes in from another branch is refused like a new one. Commits on a pull
request's branch don't need one while it is open. Before it merges, a
maintainer rewrites the branch as one commit, signs that commit with their own
key and pushes it, and then merges it with a merge commit, which GitHub signs.
So the target branch gains one commit a person signed, and one merge commit.
For a pull request from a fork, that push needs "Allow edits by maintainers".
Without it, a maintainer opens the same change from a branch in this
repository.

Bringing in upstream mempool/electrs history is not covered by this. Rewriting
it as one commit would drop upstream's lineage, so it has to land as it is,
and every commit in it then needs a verified signature. How that is done is
still to be settled.

The maintainer's push dismisses earlier approvals, and whoever pushed can't
give the new one. Someone else approves again, or an organization admin merges
the pull request through the ruleset's bypass. The bypass skips every rule in
that ruleset, and no check is required here, so wait for CI to pass on the new
commit first.

GitHub's rule accepts any verified signature, GitHub's own included. So it
can't tell a commit a person signed from one GitHub made, such as an "Update
branch" merge. The maintainers check that before they merge, and a daily audit
of merged pull requests reports any that got through.

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

Then run `apply` once for each ruleset file that changed. It updates the
ruleset of that name, or creates it when GitHub has none. For example:

```bash
apply .github/rulesets/public-main-minimal.json
```

A ruleset is found by its name. To rename one, rename it on GitHub first, then
apply the file.

`apply` is for ruleset files. Apply a change to `repository-settings.json`
with:

```bash
gh api -X PATCH "$repo" --input .github/repository-settings.json --silent
```

The merge-method setting isn't a ruleset. To set a repository up from these
files, apply `signed-commits.json` first, allow merge commits, apply the other
file, then turn squash and rebase merges off. No step leaves the repository
unable to merge, and unsigned commits can't land after the first one. The
chain stops at the first step that fails. Fix that step and run the chain
again: applying a file twice is harmless.

```bash
apply .github/rulesets/signed-commits.json &&
  gh api -X PATCH "$repo" -F allow_merge_commit=true --silent &&
  apply .github/rulesets/public-main-minimal.json &&
  gh api -X PATCH "$repo" --input .github/repository-settings.json --silent
```

## Check that GitHub matches

Run this as a repository admin. GitHub leaves the bypass actors out for anyone
else, and the check then says so instead of comparing.

```bash
repo=repos/Qbit-Org/qbit-electrs
norm='{name, target, enforcement, conditions, rules, bypass_actors} | walk(if type == "array" then sort_by(tojson) else . end)'
if live=$(gh api "$repo/rulesets?includes_parents=false&per_page=100" --jq '.[] | "\(.id)\t\(.name)"'); then
  names=$(jq -r .name .github/rulesets/*.json)
  for f in .github/rulesets/*.json; do
    name=$(jq -r .name "$f")
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
