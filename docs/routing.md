# Routing

Reference for [kilix-needle](../README.md).

## Routing: one entry point

`kilix-needle ask "<request>"`, the no-verb default, and the MCP pair
`kilix_ask_plan` / `kilix_ask_act` choose the job for a plain request. The chosen
job then runs exactly as its own command would, with its own checks, consent and
history entry. That entry gains a `route` block.

1. **Exact claims.** If exactly one job's grammar reads the whole request, it goes
   there, unless the classifier is sure of a different job. An apps reading that
   leaves another job's clause unread ("split right and open solitaire") is no claim.
2. **The router.** `router.py` is a local linear classifier: word and
   character n-grams, TF-IDF, and multinomial logistic regression. It is about
   2 MB of JSON and takes about 0.2 ms per request; with the grammar claims, a
   route takes about 1.5 ms (p95 under 5 ms). It reads only the request text.
   At probability 0.57 or above, the top job takes the request.
3. **Two jobs.** When no grammar reads the whole request and different clauses
   are each read by a different job, nothing runs, however sure the classifier
   is: the job it chose would act on its own clause and drop the other. The
   answer asks for one request per job. Clauses split at sentence ends,
   semicolons, dashes, line breaks, commas, "and" and "then" (full-width
   punctuation too, and a list's bullets are dropped), never inside quotes or
   in an agent's task after ": ". A clause's reading counts as it would at
   stage 0: not when the classifier is sure that clause is another job's. An
   apps reading that takes another job's clause as a button's name ("hide the
   clock then split right") is no claim either.
4. **Unsure.** Otherwise, below the threshold, the candidate jobs' plans run, and
   plans change nothing. If exactly one plan admits the request, its plan is
   shown, but an unsure route never acts. Otherwise the answer names the
   candidates, with one accepted phrasing each.

Routing fails closed. A missing or altered `router.json`, or a grammar that
raises anything but its own refusal, routes to no job, and nothing acts.

`ask` runs only these jobs: panes, apps, agents, files and system. It names but
never runs the others:
- **tmux** needs a socket;
- **logs** takes structured arguments;
- **pty** and **desktop** have opt-in MCP tools. Inside WSL a desktop request
  names the **windows** job instead.
A routed request is never granted access that a job's own entry point would not give.

The no-verb default always meant the panes job. A sure route sends the request
elsewhere. When the router is unsure and only the panes job's plan admits the
request, the panes job answers it as before. A clarification, two jobs' work or
a router failure runs nothing; the answer names `kilix-needle panes "<request>"`,
which sends it to the panes job explicitly. `--job JOB` skips routing.

```sh
kilix-needle ask --explain "show me htop so I can check the cpu"   # ranking and features
kilix-needle ask --interactive "close the logs pane"   # route, plan, then "Do it? [y/N]"
```

`--interactive` is what `kilix ask` runs. It acts only on an explicit yes, for
every act, and never offers one for an unsure route.

**Provenance.** The router's weights are checked in under `router-model/`, with
a training-data manifest: every source file's digest and row count, all of them
our own eval sets and agent-written rows. `router_train.py` retrains them
deterministically, and the test suite checks the retrained weights against the
checked-in ones. Current gate sets are never read. See `router-model/README.md`.
