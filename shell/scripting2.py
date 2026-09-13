"""
PyOS NOVA — Shell Scripting Language v2  (v0.0100)
====================================================
Full scripting language for .nova files stored in the SOS.

New in v2:
  · for <var> in <list>:  loops (including SOS glob expansion)
  · while <cond>:         conditional loops
  · if/elif/else:         full branching
  · try/catch:            error handling in scripts
  · Functions with return values and local variables
  · Pipe composition:  cmd1 | filter kind=code | cmd2
  · String interpolation: "Hello {name}" in any context
  · import <script>:   source another .nova script

Example .nova script::

    import /scripts/utils.nova

    def greet(name, greeting="Hello"):
        echo "{greeting}, {name}!"
        return 0

    for user in [alice bob carol]:
        result = greet {user}
        if result == 0:
            write /logs/{user}.txt "greeted at {now()}"
        else:
            notify "Failed to greet {user}" --urgency critical

    while files = ls /inbox/*.txt:
        process {files[0]}

Shell command:
    source <path>    — load and execute a .nova script
    run <path>       — same as source
    scriptcheck <p>  — syntax-check without running
"""

from __future__ import annotations

import os
import sys
import re
import time
import shlex
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from kernel.nova import NovaKernel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── Token types ───────────────────────────────────────────────────────────────

class TK:
    """Token type constants."""
    WORD   = "WORD"
    STRING = "STRING"
    NEWLINE = "NEWLINE"
    INDENT  = "INDENT"
    DEDENT  = "DEDENT"
    COLON   = "COLON"
    PIPE    = "PIPE"
    ASSIGN  = "ASSIGN"
    EOF     = "EOF"


# ── Script interpreter ────────────────────────────────────────────────────────

class ScriptContext:
    """
    Execution context for one .nova script run.

    Holds variables, functions, import path, and the shell reference.
    """

    def __init__(self, kernel: "NovaKernel",
                  parent: Optional["ScriptContext"] = None):
        """Initialise script context."""
        self.kernel    = kernel
        self.parent    = parent
        self._vars:    Dict[str, Any] = {}
        self._fns:     Dict[str, "NovaFn"] = {}
        self._output:  List[str] = []
        self._return   = None
        self._return_set = False

        # Built-in functions
        self._builtins = {
            "now":    lambda: str(int(time.time())),
            "date":   lambda: time.strftime("%Y-%m-%d"),
            "time":   lambda: time.strftime("%H:%M:%S"),
            "len":    lambda v: str(len(v)) if hasattr(v, "__len__") else "0",
            "upper":  lambda s: str(s).upper(),
            "lower":  lambda s: str(s).lower(),
            "strip":  lambda s: str(s).strip(),
            "int":    lambda s: str(int(s)),
            "float":  lambda s: str(float(s)),
            "exists": lambda p: "1" if kernel.sos.exists(p) else "0",
        }

    def get(self, name: str) -> Any:
        """Get a variable value, searching parent contexts."""
        if name in self._vars:
            return self._vars[name]
        if name in self._builtins:
            return self._builtins[name]
        if self.parent:
            return self.parent.get(name)
        return None

    def set(self, name: str, value: Any):
        """Set a variable in the current context."""
        self._vars[name] = value

    def set_fn(self, name: str, fn: "NovaFn"):
        """Register a function."""
        self._fns[name] = fn

    def get_fn(self, name: str) -> Optional["NovaFn"]:
        """Get a function, searching parent contexts."""
        if name in self._fns:
            return self._fns[name]
        if self.parent:
            return self.parent.get_fn(name)
        return None

    def interpolate(self, text: str) -> str:
        """
        Interpolate {variable} references in a string.

        Also evaluates {now()}, {date()}, etc.

        Args:
            text: String with {var} placeholders.

        Returns:
            String with placeholders replaced by values.
        """
        def _replace(m: re.Match) -> str:
            expr = m.group(1).strip()
            # Function call: name(args)
            fn_match = re.match(r"(\w+)\((.*)\)$", expr)
            if fn_match:
                fname = fn_match.group(1)
                fargs_raw = fn_match.group(2).strip()
                fargs = [a.strip().strip("'\"") for a in fargs_raw.split(",") if a.strip()]
                fn = self.get(fname)
                if callable(fn):
                    try:
                        return str(fn(*fargs))
                    except Exception:
                        return ""
            # Variable or index: name or name[0]
            idx_match = re.match(r"(\w+)\[(\d+)\]$", expr)
            if idx_match:
                val = self.get(idx_match.group(1))
                if isinstance(val, (list, tuple)):
                    try:
                        return str(val[int(idx_match.group(2))])
                    except Exception:
                        return ""
            val = self.get(expr)
            return str(val) if val is not None else ""

        return re.sub(r"\{([^}]+)\}", _replace, text)


class NovaFn:
    """A user-defined function in a .nova script."""

    def __init__(self, name: str, params: List[str],
                  body: List[str], defaults: Dict[str, str] = None):
        """Initialise a nova function."""
        self.name     = name
        self.params   = params
        self.body     = body
        self.defaults = defaults or {}


class ScriptError(Exception):
    """Raised when a .nova script encounters an error."""
    pass


class NovaInterpreter:
    """
    Interpreter for .nova shell scripts.

    Parses and executes .nova scripts with support for:
    functions, for/while/if/elif/else, try/catch, variable assignment,
    string interpolation, pipe operators, and SOS imports.
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the interpreter."""
        self.kernel = kernel

    def run_script(self, source: str,
                    ctx: Optional[ScriptContext] = None) -> ScriptContext:
        """
        Execute a .nova script.

        Args:
            source: Script source code.
            ctx:    Execution context (creates new root context if None).

        Returns:
            The execution context after running the script.
        """
        ctx = ctx or ScriptContext(self.kernel)
        lines = source.splitlines()
        self._exec_block(lines, 0, ctx)
        return ctx

    def run_sos_script(self, path: str) -> ScriptContext:
        """
        Load and execute a .nova script from the SOS.

        Args:
            path: SOS path to the script file.

        Returns:
            Execution context.
        """
        source = self.kernel.sos.read(path)
        return self.run_script(source)

    def check_syntax(self, source: str) -> List[str]:
        """
        Check syntax without executing.

        Args:
            source: Script source to check.

        Returns:
            List of error messages (empty = valid).
        """
        errors = []
        indent_stack = [0]
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(stripped)
            # Check indent consistency
            if indent > indent_stack[-1]:
                indent_stack.append(indent)
            elif indent < indent_stack[-1]:
                while indent_stack and indent_stack[-1] > indent:
                    indent_stack.pop()
                if not indent_stack or indent_stack[-1] != indent:
                    errors.append(f"Line {i}: unexpected indent {indent}")
            # Check colon-terminated block starters
            for kw in ("def ", "for ", "while ", "if ", "elif ", "else", "try", "catch"):
                if stripped.startswith(kw) and not stripped.rstrip().endswith(":"):
                    if kw not in ("else", "try", "catch"):
                        errors.append(f"Line {i}: '{kw.strip()}' block must end with ':'")
        return errors

    # ── Block execution ────────────────────────────────────────────────────────

    def _exec_block(self, lines: List[str], start_indent: int,
                     ctx: ScriptContext) -> int:
        """
        Execute a block of lines at the given indent level.

        Args:
            lines:        All script lines.
            start_indent: The indent level of this block.
            ctx:          Execution context.

        Returns:
            Index of the first line outside this block.
        """
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                i += 1
                continue

            indent = len(line) - len(stripped)
            if indent < start_indent:
                break   # end of block

            # Skip non-matching indent levels (shouldn't happen with valid input)
            if indent > start_indent:
                i += 1
                continue

            line_stripped = stripped.rstrip()

            # --- def ---
            if line_stripped.startswith("def ") and line_stripped.endswith(":"):
                i = self._parse_def(lines, i, ctx)
                continue

            # --- for ---
            if line_stripped.startswith("for ") and line_stripped.endswith(":"):
                i = self._exec_for(lines, i, start_indent, ctx)
                continue

            # --- while ---
            if line_stripped.startswith("while ") and line_stripped.endswith(":"):
                i = self._exec_while(lines, i, start_indent, ctx)
                continue

            # --- if / elif / else ---
            if line_stripped.startswith("if ") and line_stripped.endswith(":"):
                i = self._exec_if(lines, i, start_indent, ctx)
                continue

            # --- try / catch ---
            if line_stripped == "try:":
                i = self._exec_try(lines, i, start_indent, ctx)
                continue

            # --- return ---
            if line_stripped.startswith("return"):
                val = ctx.interpolate(line_stripped[6:].strip())
                ctx._return     = val
                ctx._return_set = True
                break

            # --- import ---
            if line_stripped.startswith("import "):
                path = ctx.interpolate(line_stripped[7:].strip())
                try:
                    sub_ctx = ScriptContext(self.kernel, parent=ctx)
                    self.run_sos_script(path)
                    ctx._fns.update(sub_ctx._fns)
                except Exception as e:
                    raise ScriptError(f"import {path}: {e}") from e
                i += 1
                continue

            # --- assignment: var = value ---
            assign_match = re.match(r"^(\w+)\s*=\s*(.+)$", line_stripped)
            if assign_match:
                name = assign_match.group(1)
                val  = self._eval_value(assign_match.group(2).strip(), ctx)
                ctx.set(name, val)
                i += 1
                continue

            # --- shell command ---
            self._exec_cmd(line_stripped, ctx)
            if ctx._return_set:
                break
            i += 1

        return i

    def _get_body_lines(self, lines: List[str], start: int) -> Tuple[List[str], int]:
        """Extract body lines (next indent level) starting after `start`."""
        if start + 1 >= len(lines):
            return [], start + 1
        # Determine body indent
        j = start + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            return [], j
        body_indent = len(lines[j]) - len(lines[j].lstrip())
        body = []
        while j < len(lines):
            s = lines[j]
            if not s.strip():
                j += 1; continue
            ind = len(s) - len(s.lstrip())
            if ind < body_indent:
                break
            body.append(s[body_indent:])   # dedent
            j += 1
        return body, j

    def _parse_def(self, lines: List[str], i: int,
                    ctx: ScriptContext) -> int:
        """Parse a function definition."""
        header   = lines[i].strip()[4:-1].strip()   # strip 'def ' and ':'
        # Parse name and params: name(p1, p2="default")
        m = re.match(r"(\w+)\s*\((.*)\)$", header)
        if not m:
            m = re.match(r"(\w+)", header)
            name   = m.group(1) if m else header
            params = []
        else:
            name   = m.group(1)
            params_raw = m.group(2)
            params = []
            defaults: Dict[str, str] = {}
            for p in params_raw.split(","):
                p = p.strip()
                if "=" in p:
                    pn, pv = p.split("=", 1)
                    params.append(pn.strip())
                    defaults[pn.strip()] = pv.strip().strip("'\"")
                elif p:
                    params.append(p)

        body_lines, next_i = self._get_body_lines(lines, i)
        fn = NovaFn(name, params, body_lines,
                     defaults if m and m.lastindex >= 2 else {})
        ctx.set_fn(name, fn)
        return next_i

    def _exec_for(self, lines: List[str], i: int,
                   start_indent: int, ctx: ScriptContext) -> int:
        """Execute a for loop."""
        header     = lines[i].strip()[4:-1].strip()   # strip 'for ' and ':'
        in_match   = re.match(r"(\w+)\s+in\s+(.+)$", header)
        body_lines, next_i = self._get_body_lines(lines, i)

        if not in_match:
            return next_i
        var_name  = in_match.group(1)
        iterable  = self._eval_iterable(in_match.group(2).strip(), ctx)

        for item in iterable:
            ctx.set(var_name, item)
            loop_ctx = ScriptContext(self.kernel, parent=ctx)
            loop_ctx._vars = dict(ctx._vars)
            self._exec_block(body_lines, 0, loop_ctx)
            if loop_ctx._return_set:
                ctx._return     = loop_ctx._return
                ctx._return_set = True
                break

        return next_i

    def _exec_while(self, lines: List[str], i: int,
                     start_indent: int, ctx: ScriptContext) -> int:
        """Execute a while loop."""
        cond_expr  = lines[i].strip()[6:-1].strip()   # strip 'while ' and ':'
        body_lines, next_i = self._get_body_lines(lines, i)

        for _ in range(10_000):   # safety limit
            cond_ctx = ScriptContext(self.kernel, parent=ctx)
            cond_ctx._vars = dict(ctx._vars)
            val = self._eval_value(cond_expr, ctx)
            if not val or val in ("0", "False", "false", "", "[]"):
                break
            self._exec_block(body_lines, 0, cond_ctx)
            ctx._vars.update(cond_ctx._vars)

        return next_i

    def _exec_if(self, lines: List[str], i: int,
                  start_indent: int, ctx: ScriptContext) -> int:
        """Execute an if/elif/else chain."""
        # Build list of (condition, body_lines) pairs, then else body
        branches = []
        else_body = None
        j = i

        while j < len(lines):
            stripped = lines[j].strip()
            if stripped.startswith("if ") and stripped.endswith(":"):
                cond = stripped[3:-1].strip()
                body, j = self._get_body_lines(lines, j)
                branches.append((cond, body))
            elif stripped.startswith("elif ") and stripped.endswith(":"):
                cond = stripped[5:-1].strip()
                body, j = self._get_body_lines(lines, j)
                branches.append((cond, body))
            elif stripped in ("else:", "else"):
                body, j = self._get_body_lines(lines, j)
                else_body = body
                break
            else:
                break

        # Execute first matching branch
        executed = False
        for cond, body in branches:
            if self._eval_condition(cond, ctx):
                branch_ctx = ScriptContext(self.kernel, parent=ctx)
                branch_ctx._vars = dict(ctx._vars)
                self._exec_block(body, 0, branch_ctx)
                ctx._vars.update(branch_ctx._vars)
                executed = True
                break

        if not executed and else_body:
            else_ctx = ScriptContext(self.kernel, parent=ctx)
            else_ctx._vars = dict(ctx._vars)
            self._exec_block(else_body, 0, else_ctx)
            ctx._vars.update(else_ctx._vars)

        return j

    def _exec_try(self, lines: List[str], i: int,
                   start_indent: int, ctx: ScriptContext) -> int:
        """Execute a try/catch block."""
        body_lines, j = self._get_body_lines(lines, i)
        catch_body, next_j = [], j

        if j < len(lines) and lines[j].strip().startswith("catch"):
            catch_body, next_j = self._get_body_lines(lines, j)

        try:
            try_ctx = ScriptContext(self.kernel, parent=ctx)
            try_ctx._vars = dict(ctx._vars)
            self._exec_block(body_lines, 0, try_ctx)
            ctx._vars.update(try_ctx._vars)
        except Exception as e:
            if catch_body:
                catch_ctx = ScriptContext(self.kernel, parent=ctx)
                catch_ctx._vars = {**ctx._vars, "error": str(e)}
                self._exec_block(catch_body, 0, catch_ctx)
                ctx._vars.update(catch_ctx._vars)

        return next_j

    def _exec_cmd(self, line: str, ctx: ScriptContext):
        """Execute a single shell command line."""
        # Handle pipe: cmd1 | filter
        parts    = [p.strip() for p in line.split("|") if p.strip()]
        result   = None

        for part in parts:
            # String interpolation
            part = ctx.interpolate(part)
            # Function call
            fn_match = re.match(r"^(\w+)\s+(.*)", part)
            if fn_match:
                fname = fn_match.group(1)
                fargs = fn_match.group(2).strip()
                fn = ctx.get_fn(fname)
                if fn:
                    result = self._call_fn(fn, fargs, ctx)
                    continue

            # Shell command via kernel shell
            try:
                shell = getattr(self.kernel, "shell", None)
                if shell:
                    tokens = shlex.split(part)
                    if tokens:
                        import io, contextlib
                        buf = io.StringIO()
                        with contextlib.redirect_stdout(buf):
                            shell._exec_line(part)
                        result = buf.getvalue().strip()
            except Exception as e:
                raise ScriptError(f"Command error: {part!r}: {e}") from e

    def _call_fn(self, fn: NovaFn, args_str: str,
                  ctx: ScriptContext) -> Any:
        """Call a NovaFn with arguments."""
        # Parse arguments
        raw_args = shlex.split(ctx.interpolate(args_str)) if args_str.strip() else []
        fn_ctx   = ScriptContext(self.kernel, parent=ctx)
        for j, param in enumerate(fn.params):
            if j < len(raw_args):
                fn_ctx.set(param, raw_args[j])
            elif param in fn.defaults:
                fn_ctx.set(param, fn.defaults[param])

        self._exec_block(fn.body, 0, fn_ctx)
        return fn_ctx._return

    def _eval_value(self, expr: str, ctx: ScriptContext) -> Any:
        """Evaluate an expression to a value."""
        expr = ctx.interpolate(expr.strip())
        # List literal: [a b c]
        if expr.startswith("[") and expr.endswith("]"):
            return [x.strip() for x in expr[1:-1].split() if x.strip()]
        # Quoted string
        if (expr.startswith("'") and expr.endswith("'")) or \
           (expr.startswith('"') and expr.endswith('"')):
            return expr[1:-1]
        # Number
        try:
            return int(expr)
        except ValueError:
            pass
        try:
            return float(expr)
        except ValueError:
            pass
        # Variable
        val = ctx.get(expr)
        return val if val is not None else expr

    def _eval_condition(self, expr: str, ctx: ScriptContext) -> bool:
        """Evaluate a condition expression."""
        expr = ctx.interpolate(expr.strip())
        # Comparison: a == b, a != b, a > b, a < b
        for op in ("==", "!=", ">=", "<=", ">", "<"):
            if op in expr:
                parts = expr.split(op, 1)
                left  = str(self._eval_value(parts[0].strip(), ctx))
                right = str(self._eval_value(parts[1].strip(), ctx))
                try:
                    if op == "==": return left == right
                    if op == "!=": return left != right
                    if op == ">":  return float(left) > float(right)
                    if op == "<":  return float(left) < float(right)
                    if op == ">=": return float(left) >= float(right)
                    if op == "<=": return float(left) <= float(right)
                except ValueError:
                    if op == "==": return left == right
                    if op == "!=": return left != right
        # Truthy check
        val = self._eval_value(expr, ctx)
        if isinstance(val, (list, tuple)):
            return len(val) > 0
        return bool(val) and str(val) not in ("0", "False", "false", "")

    def _eval_iterable(self, expr: str, ctx: ScriptContext) -> List[Any]:
        """Evaluate an expression as an iterable."""
        val = self._eval_value(expr, ctx)
        if isinstance(val, (list, tuple)):
            return list(val)
        # Space-separated words
        if isinstance(val, str):
            return val.split() if val else []
        return [val]
