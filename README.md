# PostgreSQL code contribution data

Data about PostgreSQL code and development contributions, stored as flat
files.

## Conventions

- Everything under `data/` is written by scripts. Don't edit it by hand;
  fix the script and regenerate.
- Files use PostgreSQL's COPY text format: tab separated, backslash
  escapes, UTF-8, with a header row. Each one loads with
  `\copy table from 'file' with (header)`.
- Values are recorded as found, not cleaned up. Normalizing names and
  deciding what counts belong downstream.

## Commit metadata

Metadata for every commit on PostgreSQL's `master` branch, extracted by
`extract-commits.py` and refreshed daily.

### Files

One file per year of each kind:

`data/commits/YYYY.tsv`, one row per commit:

| column         | source                                                    |
|----------------|-----------------------------------------------------------|
| `hash`         | `%h`, abbreviated to 11 digits (longer only if ambiguous)  |
| `committer`    | `%cn`                                                     |
| `committed_at` | `%ci`, the committer date as recorded, with its offset    |
| `insertions`   | insertions from `git log --shortstat -w -M`; 0 if none    |
| `subject`      | `%s`                                                      |

`data/tags/YYYY.tsv`, one row per structured tag in a commit message, so a
commit has as many rows as it has tags:

| column  | meaning                                              |
|---------|------------------------------------------------------|
| `hash`  | as above                                             |
| `tag`   | the key as typed, e.g. `Reviewed-by` or `Reviewed-By`  |
| `value` | the rest of the line as typed, e.g. `Jane Doe <jd@example.com>` |

Rows are in `master` order, oldest first, and tags are in the order they
appear in the message. To load them:

```sql
create table commits (hash text primary key, committer text,
                      committed_at timestamptz, insertions int, subject text);
create table tags (hash text references commits, tag text, value text);
\copy commits from 'data/commits/2025.tsv' with (header)
\copy tags from 'data/tags/2025.tsv' with (header)
```

### Rules

**Commits.** Every commit reachable from `master`. History there is linear
(no merges), so a back-patch counts once, on `master`.

**Insertions.** `-w` ignores whitespace-only changes, so a pgindent run
counts only the lines it actually changed, and `-M` makes a moved file
count as a rename rather than a fresh copy. A commit that only changes
whitespace has no stat line and gets 0.

**Years.** A commit belongs to the UTC year of its committer date. One
exception: a commit is never dated earlier than the commit before it on
`master`, because `master` only moves forward. That guards against skewed
clocks and author dates carried over as committer dates. (So far only 29
commits have gone backwards, all by less than a day, and none of those
changed year.) Using UTC means a few commits sit in a file whose year
differs from the date written in `committed_at`. For example
`2019-01-01 08:53:02 +0900` is 2018-12-31 in UTC, so it's in 2018.

**Tags.** A tag is any line of the form `Key: value`, anywhere in the
message except the first line. The key is either

- a word or hyphenated words: `Author`, `Reviewed-by`, `Backpatch-through`, or
- for older commits, a phrase of up to four words ending in `by`:
  `Submitted by`, `Reviewed by`, `Pointed out by`.

Spaces are allowed before the colon, and at least one space or tab is
required after it, which rules out URLs and times. An indented line right
after a tag continues its value. This is useful when a tag's values sit on
the following lines, as in `Discussion:` followed by a list of URLs. A
tag with no value is dropped.

Some older commits have no blank line after the first line. Their tags are
still found, but their subject (`%s`) then runs on into the rest of that
first paragraph.

The trailer convention came in gradually, so older commits are messier.
Prose such as `Patch by Jane Doe, reviewed by John Roe` isn't a tag and isn't
captured. Lines that happen to look like tags are captured, for example
`ERROR: ...` pasted from psql or `Note: ...` in a paragraph. Use the tag
name to pick the ones you want.

### Updating

The `Extract commits` Action runs daily at 08:23 UTC. It also runs when
`extract-commits.py` changes, and you can start it by hand. Each run clones
PostgreSQL from the GitHub mirror, runs `extract-commits.py`, and commits
`data/` if anything changed.

A run rewrites only the years whose list of commits has changed, which in
practice is the current year. The previous year can change too, around New
Year. A run triggered by a change to `extract-commits.py`, or a manual run with
"all" ticked, rewrites every year.

To run it locally:

```sh
python3 extract-commits.py --repo ~/pgsql --ref origin/master          # changed years
python3 extract-commits.py --repo ~/pgsql --ref origin/master --all    # everything
```

A full rewrite takes about 40 seconds on an 8-core laptop, with years
processed in parallel.
