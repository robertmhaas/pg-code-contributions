# Legacy import

Scripts left over from building `overrides/tag-fixes/` out of Robert's
2016–2025 contribution tables (`contributionsYYYY.dmp`) and the credit
prose in commit messages. The import is finished. The fix files are the
record, and further corrections are made by editing them by hand and
checking them with `check-commits.py`.

## Files

- `roles.py`: the prose credit parser. It turns sentences like "Report and
  patch by Jane Doe, reviewed by John Roe; some tweaks by me" into tags,
  with glosses, and marks a parse as confident only if every clause was
  understood and every person named got a tag. "Me" is taken to be the
  git author, who through 2025 is the committer in all but 8 commits. `split_lists.py` uses it,
  and it can be reused to parse credit in other commits.
- `split_lists.py`: replaces non-canonical credit keys (`Authors:`,
  `Reviewed by:`, `Submitted by:`, ...) with one canonical tag per person,
  and completes credit values cut off at an unindented continuation line.
  It has its own `HAND` entries for values it can't parse. It skips commits
  before 2011.
- `compare_people.py`: compares the people layer with the legacy tables.
  It recomputes how the legacy mail parser grouped pgsql-hackers messages
  by sender (with each year's `%email_map`) and reports where that grouping
  differs from the people here, `%email_map` entries whose address and name
  are different people here, and commits whose legacy author is someone
  else. `python3 tools/legacy-import/compare_people.py . contributions2025.dmp
  ~/Documents/Contributions`
