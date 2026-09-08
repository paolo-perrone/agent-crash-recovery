#!/usr/bin/env python3
"""Name every expensive call in your agent that a crash will make you pay for twice.

    python boundaries.py my_agent/            # a package, a file, or a directory
    python boundaries.py . --json             # machine-readable
    python boundaries.py --self-test          # no arguments, no services, proves both directions

Exit 0 = every expensive call sits behind its own durability boundary.
Exit 1 = at least one does not, named, with the line.

WHY THIS EXISTS
probe.py answers the same question by killing a real run, which costs you four
services, an API key and an afternoon. This reads the code you already have and
answers it in a second. It is strictly weaker: it cannot see what your framework
does at runtime, and it cannot price anything. It is also the version you will
actually run before you ship.

WHAT IT LOOKS FOR
A durability boundary is whatever your framework checkpoints: a LangGraph node, a
DBOS step, a Temporal Activity, an Inngest step.run(). Two failures matter.

  UNPROTECTED  an expensive call your orchestrator reaches with no boundary in
               between. Every retry buys it again, forever.
  SHARED       one boundary holding several expensive calls. A crash inside it
               repays all of them, so the boundary is coarser than the bill.

Expensive means it costs money or wall time: a model call, an HTTP request, a
paid API client. The list is in EXPENSIVE below and --expensive adds to it.

MEASURED FALSE-POSITIVE RATE
Swept over five real framework repositories on 2026-09-07, 4,129 Python files:
langchain-ai/langgraph, temporalio/samples-python, PrefectHQ/prefect,
dbos-inc/dbos-transact-py and hatchet-dev/hatchet. It reports six findings, and
the sweep is how eight bugs in this file were found, every one of which made
correct code look broken:

  a call graph keyed by bare function name across a whole tree (889 findings on
    one repo, all wrong), now keyed per file
  `invoke`, `ainvoke` and `predict` in the expensive list, which is LangChain's
    universal verb (38 findings), now gone
  bare vendor roots, so AsyncOpenAI() counted as a charge (4 findings), now gone
  boundaries keyed file-locally, missing every task imported from another module
  decorator markers matched as substrings, so @flow_run_app.command() was a flow
  a nested orchestrator not treated as durable, so flow-calls-flow was flagged
  clients that carry the boundary themselves, like temporalio.contrib
  a function passed to execute_activity counted as a direct call

Run it against your own corpus before you trust it on your code. That is the only
way any of the above was discovered, and fixtures found none of them.

WHAT IT CANNOT SEE
Dynamic dispatch, calls through a variable the analysis cannot resolve, anything
imported from a package it was not pointed at, and every framework not in
FRAMEWORKS. It reports what it can prove and stays quiet about the rest, so a
clean report is evidence and not a guarantee. TypeScript is read by pattern, not
by parser, and is marked as such in the output.
"""
import argparse, ast, json, os, re, sys, tempfile, textwrap

# --- what counts as expensive ------------------------------------------------
# Attribute chains and bare names. Matched against the dotted source of the call,
# so "openai" catches OpenAI().chat.completions.create through its receiver too.
# NO BARE VERBS. `invoke`, `ainvoke` and `predict` were in this list until a sweep
# over five real framework repos on 2026-09-07: they are LangChain's universal call
# verb, so they matched 38 call sites in temporalio/samples-python alone, every one
# of them correct code. A term earns its place here by naming a vendor or an API
# path, never by naming an action.
# NO BARE VENDOR ROOTS EITHER. `AsyncOpenAI(max_retries=0)` costs nothing, and
# counting it made every activity that builds a client look like two charges in
# one boundary. Four of temporalio/samples-python's SHARED findings were that,
# and all four were correct code. A vendor name earns a hit only with an API path
# behind it.
EXPENSIVE = [
    "chat.completions.create", "messages.create", "embeddings.create",
    "responses.create", "images.generate", "audio.transcriptions.create",
    "models.generate_content", "chat.send_message",
    "requests.get", "requests.post", "requests.put", "requests.delete",
    "httpx.get", "httpx.post", "httpx.request", "aiohttp",
    "boto3", "s3.upload", "s3.download", "bigquery", "stripe",
]

# Calls that CROSS a boundary rather than make one. A function handed to any of
# these executes inside the framework's own durable step, so passing it is not
# calling it, and the call graph must not draw the edge. `run() calls _run()`
# in temporalio's langsmith sample was exactly this: _run's only job is to invoke
# execute_activity.
BOUNDARY_INVOKERS = ["execute_activity", "execute_local_activity",
                     "execute_child_workflow", "start_activity", "step.run",
                     "ctx.run", "start_child_workflow"]

# --- how each framework marks a boundary -------------------------------------
# Each entry: the import that identifies it, decorator fragments that mark a
# boundary, decorator fragments that mark an orchestrator, and the callable that
# wraps a function into a boundary at runtime (DBOS.step(fn), ctx.run(fn)).
#
# The list is the product. A checker that knows four frameworks is a checker most
# readers cannot run, so adding one is a data edit and never a code edit.
FRAMEWORKS = {
    "langgraph":  {"import": "langgraph", "boundary": [], "orchestrator": [],
                   "register": ["add_node"], "name": "LangGraph node"},
    "dbos":       {"import": "dbos", "boundary": ["dbos.step"],
                   "orchestrator": ["dbos.workflow"], "register": [],
                   "wrap": ["step"], "name": "@DBOS.step()"},
    "temporal":   {"import": "temporalio", "boundary": ["activity.defn"],
                   "orchestrator": ["workflow.defn"], "register": [],
                   "name": "@activity.defn"},
    "celery":     {"import": "celery", "boundary": ["shared_task", "app.task", ".task"],
                   "orchestrator": ["chord", "chain"], "register": [],
                   "name": "@app.task"},
    "prefect":    {"import": "prefect", "boundary": ["task"], "orchestrator": ["flow"],
                   "register": [], "name": "@task"},
    "hatchet":    {"import": "hatchet_sdk", "boundary": ["hatchet.step", ".step"],
                   "orchestrator": ["hatchet.workflow"], "register": [],
                   "name": "@hatchet.step()"},
    "airflow":    {"import": "airflow", "boundary": ["task"], "orchestrator": ["dag"],
                   "register": [], "name": "@task"},
    "restate":    {"import": "restate", "boundary": [], "orchestrator": ["handler"],
                   "register": [], "ctx_run": ["ctx.run"], "name": "ctx.run()"},
}

# Clients that ARE the boundary. A plugin can route every call it receives through
# an activity or a step, so a model call on one of these is already durable even
# though the source shows a bare SDK call. Found on 2026-09-07: the Temporal Google
# GenAI samples were flagged 20 times for exactly this, and every one was correct
# code. The module prefix is the signal; the object it hands you carries the
# boundary.
WRAPPED_CLIENTS = ["temporalio.contrib"]

# Frameworks whose durability lives in configuration rather than in your code, so
# reading the source proves nothing. Named so a clean report cannot be mistaken for
# coverage the checker does not have.
CONFIG_ONLY = {
    "aws step functions": "the state machine is JSON, not Python",
    "cloudflare workflows": "steps are declared in the Worker binding",
    "azure durable functions": "the orchestrator/activity split is in host.json",
}

def _dotted(node):
    """Best-effort dotted source of a call target: OpenAI().chat.x -> openai.chat.x"""
    parts = []
    cur = node
    while True:
        if isinstance(cur, ast.Attribute):
            parts.append(cur.attr); cur = cur.value
        elif isinstance(cur, ast.Call):
            cur = cur.func
        elif isinstance(cur, ast.Name):
            parts.append(cur.id); break
        else:
            break
    return ".".join(reversed(parts)).lower()


def _is_expensive(dotted, extra):
    return any(term.lower() in dotted for term in EXPENSIVE + list(extra))


def _seg_match(dotted, marker):
    """True when `marker` is the whole dotted name or its final segment(s)."""
    return dotted == marker or dotted.endswith("." + marker)


def _decorators(fn):
    return [_dotted(d) for d in fn.decorator_list]


class FileScan:
    """One Python file: its functions, what each calls, and which are boundaries."""

    def __init__(self, path, src, extra):
        self.path, self.extra, self.src = path, extra, src
        self.tree = ast.parse(src, filename=path)
        self.frameworks = set()
        self.functions = {}        # name -> {"calls": [(callee, line)], "spend": [(dotted, line)], "node": fn}
        self.boundaries = {}       # name -> why
        self.orchestrators = {}    # name -> why
        self.alias = {}            # local name -> original def name
        self.imported = {}         # local name -> module it came from
        self.wrapped_ctors = set()  # constructors whose instances carry a boundary
        self.wrapped_vars = set()   # variables holding one
        self._imports()
        self._wrapped_vars()
        self._functions()
        self._boundaries()

    def _imports(self):
        for n in ast.walk(self.tree):
            mods = []
            if isinstance(n, ast.Import):
                mods = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods = [n.module]
            for m in mods:
                for fw, spec in FRAMEWORKS.items():
                    if m.split(".")[0] == spec["import"]:
                        self.frameworks.add(fw)
            # `from shared.agent import summarize as _summarize` renames the callee.
            # Without this the call graph loses the edge and a bypassed boundary reads
            # as clean, which is how this checker's own negative control caught it.
            if isinstance(n, ast.ImportFrom) and n.module:
                for a in n.names:
                    local = a.asname or a.name
                    self.alias[local] = a.name
                    self.imported[local] = n.module
            elif isinstance(n, ast.Import):
                for a in n.names:
                    if a.asname:
                        self.alias[a.asname] = a.name.split(".")[-1]
            if isinstance(n, ast.ImportFrom) and n.module and \
                    any(n.module.startswith(w) for w in WRAPPED_CLIENTS):
                for a in n.names:
                    self.wrapped_ctors.add(a.asname or a.name)

    def _wrapped_vars(self):
        """`client = TemporalAsyncClient()` makes every call on `client` durable."""
        if not self.wrapped_ctors:
            return
        # Two passes: an object handed out BY a wrapped client is wrapped too, and
        # it is usually assigned after the client. google_genai/chat does exactly
        # this with client.chats.create().
        for _ in range(2):
          for a in ast.walk(self.tree):
            if not isinstance(a, ast.Assign) or not isinstance(a.value, ast.Call):
                continue
            known = {c.lower() for c in self.wrapped_ctors} | self.wrapped_vars
            d = _dotted(a.value.func)
            if d.split(".")[0] in known or d in known:
                for t in a.targets:
                    if isinstance(t, ast.Name):
                        self.wrapped_vars.add(t.id.lower())

    def _functions(self):
        for fn in ast.walk(self.tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Loop context per call site. An unprotected call inside `for p in pages`
            # is not repaid once, it is repaid once per page, and that multiplier is
            # the difference between a rounding error and the whole bill.
            loops = {}
            for node in ast.walk(fn):
                it = None
                if isinstance(node, (ast.For, ast.AsyncFor)):
                    it = node.iter
                    body = list(node.body)
                elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp,
                                       ast.DictComp)):
                    it = node.generators[0].iter if node.generators else None
                    body = [node]
                else:
                    continue
                label = _dotted(it) or "the collection"
                for b in body:
                    for inner in ast.walk(b):
                        if isinstance(inner, ast.Call):
                            loops.setdefault(inner.lineno, label)

            crossed = set()
            for c in ast.walk(fn):
                if isinstance(c, ast.Call) and \
                        any(_dotted(c.func).endswith(b) for b in BOUNDARY_INVOKERS):
                    for arg in c.args:
                        if isinstance(arg, ast.Name):
                            crossed.add(arg.id)
                        elif isinstance(arg, ast.Attribute):
                            crossed.add(arg.attr)

            calls, spend = [], []
            for c in ast.walk(fn):
                if not isinstance(c, ast.Call):
                    continue
                d = _dotted(c.func)
                if not d:
                    continue
                # A call under a namespace imported from a wrapped module carries the
                # boundary too: `from temporalio.contrib import openai_agents` makes
                # every openai_agents.* call durable, and the dotted string still says
                # "openai".
                root = d.split(".")[0]
                if _is_expensive(d, self.extra) and root not in self.wrapped_vars \
                        and root not in {c.lower() for c in self.wrapped_ctors}:
                    spend.append((d, c.lineno))
                tail = d.split(".")[-1]
                if tail not in crossed:
                    calls.append((tail, c.lineno))
            # One expression, one charge. `OpenAI().chat.completions.create(...)` is
            # two Call nodes and one bill, so spends collapse by line.
            by_line = {}
            for d, line in spend:
                by_line.setdefault(line, d)
            self.crossed = getattr(self, "crossed", set()) | crossed
            self.functions[fn.name] = {"calls": calls,
                                       "spend": sorted((d, ln) for ln, d in by_line.items()),
                                       "node": fn, "line": fn.lineno, "loops": loops}

    def _boundaries(self):
        active = [FRAMEWORKS[f] for f in self.frameworks] or list(FRAMEWORKS.values())
        b_marks = [m for spec in active for m in spec.get("boundary", [])]
        o_marks = [m for spec in active for m in spec.get("orchestrator", [])]
        registers = [m for spec in active for m in spec.get("register", [])]
        wraps = [m for spec in active for m in spec.get("wrap", [])]
        ctx_runs = [m for spec in active for m in spec.get("ctx_run", [])]

        for name, info in self.functions.items():
            for dec in _decorators(info["node"]):
                # Whole segment, never substring. `m in dec` made
                # @flow_run_app.command() a Prefect flow, so a CLI command named
                # retry() was reported as an orchestrator spending outside a
                # boundary. Found on prefect's own cli/flow_run.py, 2026-09-07.
                for m in b_marks:
                    if _seg_match(dec, m):
                        self.boundaries.setdefault(name, f"@{dec}()")
                for m in o_marks:
                    if _seg_match(dec, m):
                        self.orchestrators.setdefault(name, f"@{dec}()")
        # An orchestrator marker wins over a boundary marker of the same word:
        # Prefect and Airflow both spell a boundary `@task`, and Prefect spells the
        # orchestrator `@flow`, so a function carrying both is the orchestrator.
        for name in list(self.boundaries):
            if name in self.orchestrators:
                del self.boundaries[name]

        # class-level orchestrator decorator: its methods orchestrate
        for cls in ast.walk(self.tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            decs = [_dotted(x) for x in cls.decorator_list]
            if any(_seg_match(d, m) for d in decs for m in o_marks):
                for mem in cls.body:
                    if isinstance(mem, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.orchestrators[mem.name] = "@" + decs[0] + "()"

        for c in ast.walk(self.tree):
            if not isinstance(c, ast.Call):
                continue
            d = _dotted(c.func)
            # LangGraph: add_node("x", fn) registers fn itself as the node body.
            if any(d.endswith(r) for r in registers):
                for arg in c.args:
                    if isinstance(arg, ast.Name):
                        self.boundaries[arg.id] = "add_node()"
            # Restate: ctx.run("name", fn) is the boundary, like Inngest's step.run.
            if any(d.endswith(r) for r in ctx_runs):
                for arg in c.args:
                    if isinstance(arg, ast.Name):
                        self.boundaries[arg.id] = "ctx.run()"

        # DBOS: `summarize_s = step(_summarize)` makes summarize_s the boundary and
        # leaves _summarize callable and unprotected. Registering the ARGUMENT was
        # this checker's own bug: it declared the bare function safe, so a workflow
        # calling it directly read as clean. The target is the boundary; the argument
        # only tells us what the target spends.
        for assign in ast.walk(self.tree):
            if not isinstance(assign, ast.Assign) or not wraps:
                continue
            targets = assign.targets[0]
            names = ([e.id for e in targets.elts if isinstance(e, ast.Name)]
                     if isinstance(targets, (ast.Tuple, ast.List))
                     else ([targets.id] if isinstance(targets, ast.Name) else []))
            values = (assign.value.elts if isinstance(assign.value, (ast.Tuple, ast.List))
                      else [assign.value])
            for tgt, val in zip(names, values):
                if not (isinstance(val, ast.Call)
                        and any(_dotted(val.func).endswith(w) for w in wraps)):
                    continue
                self.boundaries[tgt] = "DBOS.step()"
                if val.args and isinstance(val.args[0], ast.Name):
                    self.alias[tgt] = val.args[0].id


def _resolve(name, alias):
    """Follow an import alias back to the name the function was defined under."""
    seen = set()
    while name in alias and name not in seen:
        seen.add(name); name = alias[name]
    return name


def _spend_map(scans, extra):
    """(function key) -> True if calling it eventually spends money.

    KEYED PER FILE, not by bare name. Keying globally by name was this checker's
    worst bug: in a tree of any size some unrelated `workflow()` touches an SDK,
    and every same-named function in every other file inherits it. On
    dbos-transact-py that produced 889 findings, all 889 of them wrong. An edge
    now exists only when the callee is defined in the same file, or imported from
    a module this scan actually read.
    """
    by_module = {}
    for sc in scans:
        mod = os.path.splitext(sc.path)[0].replace(os.sep, ".")
        by_module[mod] = sc
    direct, edges = {}, {}

    def key(sc, name):
        return (sc.path, name)

    def target(sc, local):
        """Where a call in `sc` resolves: same file, or a scanned import."""
        orig = _resolve(local, sc.alias)
        if orig in sc.functions:
            return key(sc, orig)
        mod = sc.imported.get(local)
        if mod:
            tail = mod.replace(".", os.sep)
            for m, other in by_module.items():
                if m.endswith(mod) or other.path.endswith(tail + ".py") or \
                        other.path.endswith(os.path.join(tail, "__init__.py")):
                    if orig in other.functions:
                        return key(other, orig)
        return None

    for sc in scans:
        for name, info in sc.functions.items():
            direct[key(sc, name)] = bool(info["spend"])
            e = set()
            for c, _ in info["calls"]:
                t = target(sc, c)
                if t:
                    e.add(t)
            edges[key(sc, name)] = e

    spends = {k for k, v in direct.items() if v}
    changed = True
    while changed:
        changed = False
        for k, callees in edges.items():
            if k not in spends and (callees & spends):
                spends.add(k); changed = True
    return spends, target


def check_python(paths, extra):
    scans, findings = [], []
    for p in paths:
        try:
            scans.append(FileScan(p, open(p, encoding="utf-8").read(), extra))
        except SyntaxError as e:
            findings.append({"kind": "SKIPPED", "file": p, "line": e.lineno or 0,
                             "what": "could not parse", "why": str(e)})
    spends, target = _spend_map(scans, extra)
    # A boundary is (file, name), the same key shape as a spender. Keyed by bare
    # name it marked the wrong function protected; keyed file-locally it missed
    # every task imported from another module, which is how Prefect and Celery
    # codebases are actually laid out. The 2026-09-07 sweep found both, one after
    # the other, on prefect-gcp's own tests.
    boundary_keys = {(sc.path, n) for sc in scans for n in sc.boundaries}
    # A nested orchestrator is durable in its own right: a Prefect subflow and a
    # DBOS child workflow both checkpoint. Calling one is not an unprotected call,
    # and prefect's own tests are full of them.
    boundary_keys |= {(sc.path, n) for sc in scans for n in sc.orchestrators}
    # Anything called from a scanned file, imported from a module the scan never
    # read, and not obviously stdlib. A clean report over a partial tree is not a
    # clean repo, and saying so is the difference between evidence and comfort.
    defined = {n for s in scans for n in s.functions}
    unresolved = {}
    for s in scans:
        for name, info in s.functions.items():
            for c, line in info["calls"]:
                r = _resolve(c, s.alias)
                if r in defined or c not in s.imported:
                    continue
                mod = s.imported[c].split(".")[0]
                if mod in sys.stdlib_module_names or mod in {"typing", "dataclasses"}:
                    continue
                unresolved.setdefault((mod, c), (s.path, line))
    for (mod, c), (path, line) in sorted(unresolved.items()):
        findings.append({"kind": "UNREADABLE", "file": path, "line": line,
                         "what": f"{c}() comes from {mod}, which this scan did not read",
                         "why": "point the checker at that package too, or this report is "
                                "silent about whatever it spends"})

    for s in scans:
        for name, info in s.functions.items():
            in_boundary = name in s.boundaries
            # SHARED: one boundary, several expensive calls
            if in_boundary:
                spend_lines = {ln for _, ln in info["spend"]}
                reached = {(c, ln) for c, ln in info["calls"]
                           if target(s, c) in spends and ln not in spend_lines}
                n = len(info["spend"]) + len(reached)
                if n > 1:
                    findings.append({
                        "kind": "SHARED", "file": s.path, "line": info["line"],
                        "what": f"{name}() is one boundary ({s.boundaries[name]}) holding "
                                f"{n} expensive calls",
                        "why": "a crash inside it repays all of them, so the boundary is "
                               "coarser than the bill"})
                continue
            # UNPROTECTED: an orchestrator spending outside any boundary
            if name in s.orchestrators:
                for raw, line in info["calls"]:
                    t = target(s, raw)
                    if raw in s.boundaries or raw in s.orchestrators or \
                            t in boundary_keys:
                        continue
                    callee = _resolve(raw, s.alias)
                    if t in spends:
                        per = info["loops"].get(line)
                        findings.append({
                            "kind": "UNPROTECTED", "file": s.path, "line": line,
                            "per": per, "callee": callee,
                            "what": f"{name}() calls {raw}() with no boundary",
                            "why": f"{s.orchestrators[name]} replays from the top, so this "
                                   "is bought again on every retry"})
                for d, line in info["spend"]:
                    findings.append({
                        "kind": "UNPROTECTED", "file": s.path, "line": line,
                        "per": info["loops"].get(line),
                        "what": f"{name}() calls {d} directly",
                        "why": f"{s.orchestrators[name]} replays from the top, so this is "
                               "bought again on every retry"})
    return findings, sorted({fw for s in scans for fw in s.frameworks}), scans


# --- TypeScript, by pattern and honest about it ------------------------------
TS_FN = re.compile(r"createFunction\s*\(", re.S)
TS_STEP = re.compile(r"step\.run\s*\(\s*[`\"'][^`\"']*[`\"']\s*,\s*\(\)\s*=>\s*([A-Za-z_$][\w$]*)")
TS_AWAIT = re.compile(r"(?<!step\.run\()\bawait\s+([A-Za-z_$][\w$]*)\s*\(")


def check_typescript(paths):
    findings = []
    for p in paths:
        src = open(p, encoding="utf-8").read()
        if not TS_FN.search(src):
            continue
        wrapped = set(TS_STEP.findall(src))
        for m in TS_AWAIT.finditer(src):
            name = m.group(1)
            if name in ("Promise", "fetch") or name in wrapped:
                continue
            # inside a step.run callback on the same line? then it is wrapped
            line_start = src.rfind("\n", 0, m.start()) + 1
            if "step.run" in src[line_start:m.start()]:
                continue
            findings.append({
                "kind": "UNPROTECTED", "file": p,
                "line": src[:m.start()].count("\n") + 1,
                "what": f"await {name}() outside step.run()",
                "why": "the function body re-runs once per step, so anything outside a "
                       "step executes on every invocation"})
    return findings


def collect(target):
    py, ts = [], []
    if os.path.isfile(target):
        (py if target.endswith(".py") else ts).append(target)
        return py, ts
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in
                   {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}]
        for f in files:
            p = os.path.join(root, f)
            if f.endswith(".py") and f != os.path.basename(__file__):
                py.append(p)
            elif f.endswith((".ts", ".tsx")):
                ts.append(p)
    return sorted(py), sorted(ts)


def bill(findings, cost, fanout):
    """What one crash costs, from what the source can prove.

    Fixed sites are counted exactly. A site inside a loop is counted at `fanout`,
    which the reader passes because the source cannot know how long the list is.
    Both halves are printed, so nobody mistakes an assumption for a measurement."""
    fixed = [f for f in findings if f["kind"] == "UNPROTECTED" and not f.get("per")]
    looped = [f for f in findings if f["kind"] == "UNPROTECTED" and f.get("per")]
    calls = len(fixed) + len(looped) * fanout
    def plural(n, w):
        return f"{n} {w}" + ("" if n == 1 else "s")

    def money(x):
        return f"${x:,.2f}" if x >= 0.01 else f"${x:.4f}"

    lines = []
    if looped:
        terms = ", ".join(sorted({f"once per item in {f['per']}" for f in looped}))
        lines.append(f"  every retry repays {plural(len(fixed), 'fixed call')} plus {terms}.")
    elif fixed:
        lines.append(f"  every retry repays {plural(len(fixed), 'call')}.")
    if calls and cost is not None:
        basis = (f"at {fanout} items per loop and {money(cost)} a call"
                 if looped else f"at {money(cost)} a call")
        lines.append(f"  {basis}, one crash repays {plural(calls, 'call')}, "
                     f"{money(calls * cost)}. A thousand crashes: "
                     f"{money(calls * cost * 1000)}.")
    elif calls:
        lines.append(f"  pass --cost to price it: {plural(calls, 'call')} per retry"
                     + (f" at {fanout} items per loop" if looped else ""))
    return lines



# --- --fix: the edit, not just the finding ------------------------------------
# What each framework needs to protect a bare function, and whether a decorator
# alone is enough. Where it is not, --fix prints the change and refuses to make
# it, because a half-correct edit to durability code is worse than a report.
FIXABLE = {
    "dbos":    {"decorator": "@DBOS.step()", "import": ("dbos", "DBOS")},
    "prefect": {"decorator": "@task", "import": ("prefect", "task")},
    "celery":  {"decorator": "@shared_task", "import": ("celery", "shared_task")},
    "airflow": {"decorator": "@task", "import": ("airflow.decorators", "task")},
}
MANUAL = {
    "temporal": "an @activity.defn decorator is half of it; the workflow must also "
                "call it through workflow.execute_activity()",
    "langgraph": "a node is registered with add_node(), not declared with a decorator",
    "hatchet": "steps are methods on the workflow class, so the fix is a move, not a "
               "decorator",
    "restate": "the call has to move inside ctx.run(), which is a call-site rewrite",
    "inngest": "the call has to move inside step.run(), which is a call-site rewrite",
}


def _defining_file(name, scans):
    for sc in scans:
        if name in sc.functions:
            return sc
    return None


def plan_fixes(findings, scans, frameworks):
    """(edits, manual). An edit is (path, insert_line, text, why)."""
    edits, manual = [], []
    fw = next((f for f in frameworks if f in FIXABLE), None)
    for f in findings:
        if f["kind"] != "UNPROTECTED":
            continue
        target = f.get("callee")
        if not target:
            manual.append((f, "the call site names no function this could decorate"))
            continue
        if fw is None:
            why = next((MANUAL[k] for k in frameworks if k in MANUAL),
                       "no framework here declares boundaries with a decorator")
            manual.append((f, why))
            continue
        sc = _defining_file(target, scans)
        if sc is None:
            manual.append((f, f"{target}() is defined outside the files this scan read"))
            continue
        spec = FIXABLE[fw]
        mod, sym = spec["import"]
        has_import = any(sym == local or sym == orig
                         for local, orig in sc.alias.items()) or \
                     any(sym in line for line in sc.src.split("\n")[:40]
                         if line.startswith(("import ", "from ")))
        if not has_import:
            manual.append((f, f"{sc.path} does not import {sym} from {mod}; add the "
                              f"import and re-run --fix"))
            continue
        line = sc.functions[target]["line"]
        indent = " " * (len(sc.src.split("\n")[line - 1])
                        - len(sc.src.split("\n")[line - 1].lstrip()))
        edits.append((sc.path, line, indent + spec["decorator"],
                      f"{target}() gets {spec['decorator']}, so its result is "
                      "checkpointed and the retry reads it back"))
    return edits, manual


def apply_fixes(edits):
    by_file = {}
    for path, line, text, _ in edits:
        by_file.setdefault(path, []).append((line, text))
    for path, items in by_file.items():
        lines = open(path, encoding="utf-8").read().split("\n")
        for line, text in sorted(items, reverse=True):
            lines.insert(line - 1, text)
        open(path, "w", encoding="utf-8").write("\n".join(lines))
    return len(edits)


def render_fixes(edits, manual, root):
    if edits:
        print("  edits this can make:\n")
        for path, line, text, why in edits:
            print(f"  {os.path.relpath(path, root)}:{line}")
            print(f"    + {text.strip()}")
            print(f"      {why}\n")
    if manual:
        print("  edits this refuses to make:\n")
        for f, why in manual:
            print(f"  {os.path.relpath(f['file'], root)}:{f['line']}  {f['what']}")
            print(f"      {why}\n")
    if edits:
        print("  --fix --write applies the first list and leaves the second alone.")


def render(findings, frameworks, ts_count, root, cost=None, fanout=10):
    if frameworks:
        print(f"  frameworks seen: {', '.join(frameworks)}")
    if ts_count:
        print(f"  {ts_count} TypeScript file(s) read by pattern, not by parser: "
              "treat those lines as a prompt to look, not a verdict")
    print()
    if not findings:
        print("  every expensive call this could see sits behind its own boundary.")
        print("  That is evidence, not a guarantee: read WHAT IT CANNOT SEE "
              "in the header.")
        return 0
    order = {"UNPROTECTED": 0, "SHARED": 1, "UNREADABLE": 2, "SKIPPED": 3}
    for f in sorted(findings, key=lambda f: (order.get(f["kind"], 9), f["file"], f["line"])):
        rel = os.path.relpath(f["file"], root)
        print(f"  {f['kind']:<12} {rel}:{f['line']}")
        print(f"               {f['what']}")
        if f.get("per"):
            print(f"               repaid once per item in {f['per']}")
        print(f"               {f['why']}")
        print()
    bad = [f for f in findings if f["kind"] in ("UNPROTECTED", "SHARED")]
    n_un = sum(1 for f in bad if f["kind"] == "UNPROTECTED")
    n_sh = len(bad) - n_un
    bits = []
    if n_un:
        bits.append(f"{n_un} call(s) your orchestrator buys again on every retry")
    if n_sh:
        bits.append(f"{n_sh} boundary(ies) holding more than one expensive call")
    print("  " + ", ".join(bits) + ".")
    for line in bill(findings, cost, fanout):
        print(line)
    return 1 if bad else 0


# --- self-test ---------------------------------------------------------------
GOOD = '''
from dbos import DBOS
from openai import OpenAI

@DBOS.step()
def summarize(page):
    return OpenAI().chat.completions.create(model="gpt-4o-mini", messages=[])

@DBOS.workflow()
def research(pages):
    return [summarize(p) for p in pages]
'''

UNPROTECTED = '''
from dbos import DBOS
from openai import OpenAI

def summarize(page):
    return OpenAI().chat.completions.create(model="gpt-4o-mini", messages=[])

@DBOS.workflow()
def research(pages):
    return [summarize(p) for p in pages]
'''

SHARED = '''
from dbos import DBOS
from openai import OpenAI

@DBOS.step()
def summarize_and_outline(pages):
    a = OpenAI().chat.completions.create(model="gpt-4o-mini", messages=[])
    b = OpenAI().chat.completions.create(model="gpt-4o-mini", messages=[])
    return a, b

@DBOS.workflow()
def research(pages):
    return summarize_and_outline(pages)
'''

TEMPORAL_MANUAL = '''
from temporalio import workflow
from openai import OpenAI

def outline(summaries):
    return OpenAI().chat.completions.create(model="m", messages=[])

@workflow.defn
class Research:
    @workflow.run
    async def run(self, pages):
        return outline(pages)
'''

TS_CASE = '''
import { Inngest } from "inngest"
import { summarize, outline } from "./agent"
export const inngest = new Inngest({ id: "x" })
export const research = inngest.createFunction({ id: "r" }, { event: "e" },
  async ({ event, step }) => {
    const s = await step.run("summarize", () => summarize(event.data.page))
    const o = await outline([s])
    return o
  })
'''


PREFECT = '''
from prefect import flow, task
from openai import OpenAI

@task
def summarize(page):
    return OpenAI().chat.completions.create(model="m", messages=[])

def outline(s):
    return OpenAI().chat.completions.create(model="m", messages=[])

@flow
def research(pages):
    return outline([summarize(p) for p in pages])
'''

CELERY = '''
from celery import shared_task
from openai import OpenAI

@shared_task
def summarize(page):
    return OpenAI().chat.completions.create(model="m", messages=[])
'''

FANOUT = '''
from dbos import DBOS
from openai import OpenAI

def summarize(page):
    return OpenAI().chat.completions.create(model="m", messages=[])

@DBOS.workflow()
def research(pages):
    out = []
    for p in pages:
        out.append(summarize(p))
    return out
'''


def self_test():
    cases = [("a step around the model call", GOOD, 0, []),
             ("the same call with no step", UNPROTECTED, 1, ["UNPROTECTED"]),
             ("two model calls in one step", SHARED, 1, ["SHARED"]),
             ("prefect: @task protects, a bare helper does not", PREFECT, 1, ["UNPROTECTED"]),
             ("celery: @shared_task is a boundary", CELERY, 0, [])]
    ok = True
    with tempfile.TemporaryDirectory() as d:
        for label, src, want, kinds in cases:
            p = os.path.join(d, "case.py")
            open(p, "w").write(textwrap.dedent(src))
            findings, fw, _ = check_python([p], [])
            got = 1 if [f for f in findings if f["kind"] in ("UNPROTECTED", "SHARED")] else 0
            seen = sorted({f["kind"] for f in findings})
            good = got == want and all(k in seen for k in kinds)
            ok &= good
            print(f"  {'ok  ' if good else 'FAIL'}  {label}: exit {got} (wanted {want}), {seen}")
        # the multiplier, which is the difference between a rounding error and a bill
        p = os.path.join(d, "fanout.py")
        open(p, "w").write(textwrap.dedent(FANOUT))
        findings, _, _ = check_python([p], [])
        per = [f.get("per") for f in findings if f["kind"] == "UNPROTECTED"]
        good = per == ["pages"]
        ok &= good
        print(f"  {'ok  ' if good else 'FAIL'}  a call inside `for p in pages` is priced "
              f"per item: {per}")
        priced = bill(findings, 0.0004, 11)
        good = any("11 calls" in l and "$0.0044" in l for l in priced)
        ok &= good
        print(f"  {'ok  ' if good else 'FAIL'}  the bill multiplies by --fanout: "
              f"{priced[-1].strip() if priced else 'no bill'}")

        # --fix, both directions: it edits what it can prove and refuses the rest
        p = os.path.join(d, "fixme.py")
        open(p, "w").write(textwrap.dedent(UNPROTECTED))
        findings, fw, scans = check_python([p], [])
        edits, manual = plan_fixes(findings, scans, fw)
        good = len(edits) == 1 and "@DBOS.step()" in edits[0][2] and not manual
        ok &= good
        print(f"  {'ok  ' if good else 'FAIL'}  --fix plans the decorator DBOS needs: "
              f"{[e[2].strip() for e in edits]}")
        apply_fixes(edits)
        findings2, _, _ = check_python([p], [])
        good = not [f for f in findings2 if f["kind"] in ("UNPROTECTED", "SHARED")]
        ok &= good
        print(f"  {'ok  ' if good else 'FAIL'}  the file it wrote comes back clean")

        p = os.path.join(d, "refuse.py")
        open(p, "w").write(textwrap.dedent(TEMPORAL_MANUAL))
        before = open(p).read()
        findings, fw, scans = check_python([p], [])
        edits, manual = plan_fixes(findings, scans, fw)
        apply_fixes(edits)
        good = not edits and len(manual) == 1 and open(p).read() == before
        ok &= good
        print(f"  {'ok  ' if good else 'FAIL'}  --fix refuses Temporal and touches nothing: "
              f"{[m[1][:38] for m in manual]}")

        p = os.path.join(d, "case.ts")
        open(p, "w").write(textwrap.dedent(TS_CASE))
        f = check_typescript([p])
        good = any("outline" in x["what"] for x in f)
        ok &= good
        print(f"  {'ok  ' if good else 'FAIL'}  an await outside step.run() in TypeScript: "
              f"{[x['what'] for x in f]}")
    print("\nself-test PASS" if ok else "\nself-test FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("target", nargs="?", default=".", help="file, directory or package")
    ap.add_argument("--expensive", action="append", default=[],
                    help="extra call fragment to treat as expensive; repeatable")
    ap.add_argument("--cost", type=float, default=None, metavar="USD",
                    help="price of one expensive call, so the report gives a bill instead "
                         "of a count. gpt-4o-mini at a few hundred tokens is about 0.0004")
    ap.add_argument("--fanout", type=int, default=10, metavar="N",
                    help="items to assume in a loop the source cannot size (default 10)")
    ap.add_argument("--fix", action="store_true",
                    help="show the edit that would protect each call, and say which ones "
                         "this refuses to make and why")
    ap.add_argument("--write", action="store_true",
                    help="with --fix, apply the safe edits in place")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    py, ts = collect(a.target)
    if not py and not ts:
        print(f"  nothing to read under {a.target}")
        return 0
    findings, frameworks, scans = check_python(py, a.expensive)
    findings += check_typescript(ts)
    if a.json:
        print(json.dumps({"frameworks": frameworks, "findings": findings}, indent=1))
        return 1 if [f for f in findings if f["kind"] in ("UNPROTECTED", "SHARED")] else 0
    root = a.target if os.path.isdir(a.target) else os.path.dirname(a.target) or "."
    rc = render(findings, frameworks, len(ts), root, a.cost, a.fanout)
    if a.fix:
        print()
        edits, manual = plan_fixes(findings, scans, frameworks)
        render_fixes(edits, manual, root)
        if a.write and edits:
            n = apply_fixes(edits)
            print(f"\n  wrote {n} edit(s). Re-run without --fix to see what is left.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
