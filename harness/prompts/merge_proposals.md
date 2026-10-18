You keep the proposal list of an automated coding harness tidy. Below are the open proposals of the project `{project}`, waiting for the owner's approval (one JSON object per line).

Find proposals that overlap: the same topic, the same change from another angle, or one being a part of another, so that one pull request should do them together. Do not group proposals that only share a file or a broad area but would be separate changes. The merged task must still fit one pull request of at most {max_diff_lines} changed lines; if it would not, do not merge.

Proposals:
{proposals}

Answer with ONLY a JSON array, one object per group of 2 or more proposals ([] when nothing overlaps, which is common):
[{{"ids": ["...", "..."], "title": "...", "description": "...", "why": "...", "done": "..."}}]
Write title, description, why and done in Czech. title: at most 80 characters. description: the merged task, what to change where (files), keeping the concrete details of the originals. why: one sentence on what the originals share. done: a concrete check that proves the merged task is finished.
