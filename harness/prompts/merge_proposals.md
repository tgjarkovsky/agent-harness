You keep the proposal list of an automated coding harness tidy. Below are the open proposals of the project `{project}`, waiting for the owner's approval (one JSON object per line).

Find proposals that are really the same task: they fix the same bug, deliver the same user-visible outcome, or one is a part of the other. Merge only proposals of the same kind. These are NOT reasons to merge: touching the same file, function or page; a shared helper or refactor; avoiding conflicts between pull requests; a similar area or theme. If you hesitate, do not merge. The merged task must still fit one pull request of at most {max_diff_lines} changed lines; if it would not, do not merge.

Proposals:
{proposals}

Answer with ONLY a JSON array, one object per group of 2 or more proposals ([] when nothing overlaps, which is common):
[{{"ids": ["...", "..."], "title": "...", "description": "...", "why": "...", "done": "..."}}]
Write title, description, why and done in Czech. title: at most 80 characters. description: the merged task, what to change where (files), keeping the concrete details of the originals. why: one sentence on what the originals share. done: a concrete check that proves the merged task is finished.
