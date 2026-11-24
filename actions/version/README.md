# version

Release versioning for the deploy workflows of the optimaru repositories.

| Deploy | Tag | `pyproject.toml` | `package.json` | GitHub release |
| --- | --- | --- | --- | --- |
| prod | `vX.Y.Z`: the latest `vX.Y.Z` raised by the chosen bump (major, minor, patch) | `X.Y.Z`, committed to `main` | `X.Y.Z`, committed to `main` | yes, notes generated since the previous release |
| stage | `vX.Y.Z-stageN`: the latest `vX.Y.Z`, N counting up from 0 | `X.Y.Z+stageN` in the build only | `X.Y.Z-stageN` in the build only | no |

With no `vX.Y.Z` tag yet, the version in the first listed file is the base. A `uv.lock`
or `package-lock.json` next to a versioned file follows it. Tags and the prod commit are
pushed only after the deploy succeeded.

```yaml
permissions:
  contents: write
steps:
  - uses: actions/checkout@v4
  - id: version
    uses: optimaru/.github/actions/version@main
    with:
      environment: ${{ inputs.environment == 'prod' && 'prod' || 'stage' }}
      bump: ${{ inputs.bump }}
      files: |
        pyproject.toml
  # ... build and deploy ...
  - uses: optimaru/.github/actions/version/publish@main
    with:
      environment: ${{ inputs.environment == 'prod' && 'prod' || 'stage' }}
      tag: ${{ steps.version.outputs.tag }}
      previous_tag: ${{ steps.version.outputs.previous_tag }}
      existing: ${{ steps.version.outputs.existing }}
      files: ${{ steps.version.outputs.files }}
```

A run on a `vX.Y.Z` tag ref with `environment: prod` deploys that release as it is
(`existing` is true) and only creates its GitHub release if it is missing. Repositories
whose prod deploys run from tags use this: a dispatch workflow raises the version with
`publish` and `release: false`, then starts the deploy on the new tag.

The prod version commit is pushed to `main` with `GITHUB_TOKEN`, which starts no other
workflow. If `main` moved meanwhile the commit is replayed on top; if `main` refuses the
push (a ruleset without a bypass for GitHub Actions), the tag and release still go out and
a warning says to raise the files in a PR.
