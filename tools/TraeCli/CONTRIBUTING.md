# Contributing

## Git commit convention

This repository uses Conventional Commits and validates the commit header with a
versioned `commit-msg` hook.

Install the local Git configuration after cloning:

```bash
./scripts/install-git-hooks.sh
```

The script configures:

- `core.hooksPath=.githooks`
- `commit.template=.gitmessage.txt`

Commit header format:

```text
<type>(<scope>): <subject>
```

Examples:

- `feat(chat): add inspect fallback output`
- `fix(state): handle missing storage.json`
- `docs(readme): clarify install steps`

Allowed types:

- `feat`
- `fix`
- `docs`
- `style`
- `refactor`
- `perf`
- `test`
- `build`
- `ci`
- `chore`
- `revert`

Guidelines:

- Keep the scope short and specific when it adds useful context
- Write the subject in imperative mood
- Keep the first line within 72 characters
- Use the body to explain intent, tradeoffs, or migration notes
- Use `!` or a `BREAKING CHANGE:` footer for incompatible changes

The hook allows Git-generated merge/revert messages plus `fixup!` and
`squash!` commits for local history editing.

## Before opening a PR

Run the project test suite from `agent-harness`:

```bash
cd agent-harness
python3 -m unittest discover -s cli_anything/trae/tests -v
```

Do not commit local virtual environments or other generated artifacts.
