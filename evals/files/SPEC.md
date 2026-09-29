# kilix-needle files job: evaluation set specification

A request is one thing a person types to find files on their own computer. The assistant
may only READ, and only inside one named place (a scope). The answer is the list of queries
it should run, or an empty list when it should run nothing. Every answer has at most one
query.

## Line format

One JSON object per line:

```json
{"request": "list recent files in my Documents", "expect": [["list_files", {"scope": "Documents", "order": "modified"}]], "tag": "recent"}
```

- `request`: the words the person types.
- `expect`: `[]`, or a list with exactly one `[query, arguments]` pair.
- `tag`: one of the tags below.

## Scopes

Every query needs a scope, and the request must name it. The scope is one of:

| The person says | `scope` |
|---|---|
| here, this directory, the current directory, this project | `here` |
| Downloads, my downloads | `Downloads` |
| Documents, my documents | `Documents` |
| research | `research` |
| projects | `projects` |
| an explicit directory starting with `/`, `~/` or `./` | that path, exactly as written |

No scope named ("find my pdfs") means `[]`. So does an unbounded one ("everywhere",
"my whole disk", "the computer"). A path with `..` or `.` components is `[]`.

## The four queries

All arguments are always given.

| Query | Arguments | Meaning |
|---|---|---|
| `find_files` | `scope`; `name`: a literal piece of the file name, or `""`; `extension`: lower-case extension without the dot, or `""`; `modified`: `any`, `today`, `yesterday` or `last N days` (N 1 to 99) | Find files by name substring, extension and/or modification date |
| `search_text` | `scope`; `text`: the literal text to look for | Find text files containing that exact text |
| `list_files` | `scope`; `order`: `size` (largest) or `modified` (newest) | List the largest or most recently changed files |
| `preview_file` | `scope`; `path`: one relative path inside the scope | Show the start of one text file |

Value rules:

- `name`, `text` and `path` are literal, copied exactly (case kept) from the request. They
  must be clearly delimited, normally by quotes. If it is unclear exactly which characters
  are the literal, the answer is `[]`.
- `extension` is the file type as an extension: "pdfs"/"PDF files" → `pdf`, "python
  files" → `py`, "markdown" → `md`, "text files" → `txt`, "images"/"pictures" → `[]` (more
  than one extension).
- "this week" → `last 7 days`; "in the past 3 days" → `last 3 days`.
- `preview_file` `path` is relative, with no `..`, no leading `/` and no hidden (`.`)
  components.

## When the answer is `[]`

- Anything that changes files: delete, move, rename, copy, zip, edit, open in an editor,
  send, upload, email. Also when a read is combined with a change or a second request.
- No scope, or an unbounded one.
- Hidden files, semantic or fuzzy search ("similar to", "about taxes"), size thresholds
  ("over 1 GB"), sorting other than largest/newest, counts, or anything else outside the
  table.
- Negated, cancelled, conditional or scheduled requests.
- A vague target: "the PDF I downloaded", "that file".

## Tags

`name`, `type`, `date`, `text`, `size`, `recent`, `preview`, `scope` (explicit path
scopes), `refusal` (expect `[]`).

## Writing rules

- Write the way real people type: terse, chatty, lower case, typos, questions, commands.
  Vary the words: do not reuse one template with a different noun.
- Every request must have exactly one correct answer under this spec. If you are unsure,
  rewrite the request until you are sure, or leave it out.
