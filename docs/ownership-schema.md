# `ownership.yml` schema — AgenticOS group-coding flow

`ownership.yml` lives at the root of each participating repo. It maps path
globs to the agent(s) that **own** (implement) code under those paths and the
agent(s) required to **review** it. The [CEO decomposition routine](https://github.com/EngineeringMoonBear/AgenticOS/issues/249)
(GOL-154) reads these files to turn a spec into D2-partitioned, owned,
reviewable child issues:

> given a spec, intersect the spec's touched paths with each repo's
> `ownership.yml` `paths` entries → emit **one issue per owned partition**,
> carrying acceptance criteria, `owner` + `review` (routing), and a `wave`.

Canonical copy: this file. Mirror in the Obsidian vault wiki page
`Software/AgenticOS Group Coding Flow`.

## Fields

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `version` | int | yes | Schema version. Currently **1**. |
| `repo` | string | yes | Repo name — a sanity check that the file matches its home. |
| `defaults` | map | no | Repo-wide defaults. Only `defaults.review` (a handle list) is defined; used as the reviewer fallback when a partition omits `review`. |
| `partitions` | list | yes | Non-empty list of partitions (below). |

### Partition entry

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `name` | string | yes | Unique partition id within the file. Becomes the emitted issue's label/slug. |
| `paths` | list&lt;glob&gt; | yes | Non-empty list of gitignore-style globs. The partition boundary. A path may be claimed by **only one** partition (no duplicate globs). |
| `owner` | handle \| list&lt;handle&gt; | yes | Implementing agent(s). |
| `review` | list&lt;handle&gt; | yes\* | Required reviewer(s) for review routing. \*May be omitted if `defaults.review` is set. |
| `wave` | int | no | Sequencing wave for the decomposition routine (lower runs first; shared libraries typically wave 1, apps wave 2, docs later). |

A **handle** is a Paperclip agent url-key matching `^[a-z0-9][a-z0-9-]*$`
(e.g. `engineering-alice`, `frontend-iris`, `devops-terra`). In other
deployments the same slot can hold a GitHub handle or team slug.

## Coverage rule

Every primary source **directory** at the repo root must be claimed by at least
one partition glob — the linter fails on any unmapped top-level source dir.
Build output, dependency, VCS, and IDE directories (`node_modules`, `dist`,
`.git`, `.github`, `.turbo`, `.venv`, …) are exempt. Root-level files are not
required to be mapped but may be (e.g. `docker-compose.*.yml`).

## Grammar (parser subset)

The linter is dependency-free and parses a strict YAML subset:

- 2-space indentation, **spaces only** (tabs in indentation are rejected).
- Block mappings (`key: value`) and block/flow sequences (`- item` / `[a, b]`).
- Scalars: bare strings, quoted strings, integers, `true`/`false`, `null`/`~`.
- `#` line/inline comments.

This keeps the files trivially machine-readable for the decomposition routine.

## Validate

```bash
python3 scripts/lint-ownership.py            # validates ./ownership.yml
python3 scripts/lint-ownership.py --file path/to/ownership.yml --root path/to/repo
```

Exit code `0` = valid, `1` = validation errors, `2` = usage/parse error. Run in
CI via the `ownership-lint` workflow (`.github/workflows/ownership-lint.yml`).
