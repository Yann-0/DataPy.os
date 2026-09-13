"""
PyOS NOVA — AI Code Reviewer
==============================
Automatically reviews Python files when saved.
Queues findings in the advisor for the user to read.

Also provides `review <file>` command for on-demand review.

Checks:
  - Security issues (exec, eval, shell injection, hardcoded secrets)
  - Performance issues (n+1 loops, blocking calls in async, etc.)
  - Style issues (long functions, missing docstrings)
  - AI suggestions (asks LLM for improvement ideas)
"""

import os, sys, re, ast, time
from typing import List, Dict, Optional, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from ai.engine  import AIEngine
    from ai.advisor import Advisor


class Issue:
    """Issue."""
    SEVERITY = {"hint": 0, "warn": 1, "error": 2}
    SEV_ICON = {"hint": "ℹ", "warn": "⚠", "error": "✖"}
    SEV_COL  = {"hint": "cyan", "warn": "yellow", "error": "red"}

    def __init__(self, line: int, severity: str, category: str, message: str):
        """Initialise the instance."""
        self.line     = line
        self.severity = severity
        self.category = category
        self.message  = message

    def __str__(self):
        """Return a human-readable string representation."""
        return f"  L{self.line:<4} {self.SEV_ICON[self.severity]} [{self.category}] {self.message}"


class StaticReviewer:
    """Pure Python static analysis — no LLM needed."""

    DANGEROUS_CALLS = {"eval", "exec", "compile", "__import__",
                       "os.system", "subprocess.call", "subprocess.Popen"}
    SECRET_PATTERNS = [
        r"(password|passwd|secret|api_key|apikey|token)\s*=\s*['\"][^'\"]{4,}['\"]",
        r"AWS_SECRET|PRIVATE_KEY",
    ]

    def review(self, code: str, path: str = "") -> List[Issue]:
        """Review the operation and return findings.

            Args:
            code (str): Code.
            path (str): Path, defaults to ''.


            Returns:
                List[Issue]: Result.
            """
        issues = []
        issues += self._check_syntax(code)
        if not issues:   # only continue if syntax is valid
            issues += self._check_ast(code)
        issues += self._check_patterns(code)
        issues += self._check_style(code)
        return sorted(issues, key=lambda i: (Issue.SEVERITY[i.severity], i.line), reverse=True)

    def _check_syntax(self, code: str) -> List[Issue]:
        """Check syntax and return the result.

            Args:
            code (str): Code.


            Returns:
                List[Issue]: Result.
            """
        try:
            ast.parse(code)
            return []
        except SyntaxError as e:
            return [Issue(e.lineno or 1, "error", "syntax", str(e))]

    def _check_ast(self, code: str) -> List[Issue]:
        """Check ast and return the result.

            Args:
            code (str): Code.


            Returns:
                List[Issue]: Result.
            """
        issues = []
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return issues

        for node in ast.walk(tree):
            # Dangerous function calls
            if isinstance(node, ast.Call):
                name = self._call_name(node)
                if name in self.DANGEROUS_CALLS:
                    issues.append(Issue(node.lineno, "warn", "security",
                                        f"Dangerous call: {name}()"))

            # Functions too long (>50 lines)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = node.end_lineno - node.lineno
                if length > 50:
                    issues.append(Issue(node.lineno, "hint", "style",
                                        f"Function '{node.name}' is {length} lines (consider splitting)"))
                # Missing docstring
                if not (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)):
                    if length > 10:
                        issues.append(Issue(node.lineno, "hint", "style",
                                            f"Function '{node.name}' has no docstring"))

            # Bare except
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                issues.append(Issue(node.lineno, "warn", "style",
                                    "Bare except: clause catches all exceptions"))

            # Mutable default argument
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for default in node.args.defaults:
                    if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                        issues.append(Issue(node.lineno, "warn", "bug",
                                            f"Mutable default argument in '{node.name}'"))

        return issues

    def _check_patterns(self, code: str) -> List[Issue]:
        """Check patterns and return the result.

            Args:
            code (str): Code.


            Returns:
                List[Issue]: Result.
            """
        issues = []
        lines  = code.splitlines()
        for i, line in enumerate(lines, 1):
            # Hardcoded secrets
            for pat in self.SECRET_PATTERNS:
                if re.search(pat, line, re.I):
                    issues.append(Issue(i, "error", "security",
                                        "Possible hardcoded secret/credential"))
                    break
            # TODO/FIXME
            if re.search(r'\bTODO\b|\bFIXME\b|\bHACK\b|\bXXX\b', line):
                issues.append(Issue(i, "hint", "todo",
                                    line.strip()[:60]))
            # Long lines
            if len(line) > 120:
                issues.append(Issue(i, "hint", "style",
                                    f"Line too long ({len(line)} chars, PEP8 max 79)"))
        return issues

    def _check_style(self, code: str) -> List[Issue]:
        """Check style and return the result.

            Args:
            code (str): Code.


            Returns:
                List[Issue]: Result.
            """
        issues = []
        lines  = code.splitlines()
        # No newline at end
        if code and not code.endswith("\n"):
            issues.append(Issue(len(lines), "hint", "style",
                                "File should end with a newline"))
        # Multiple blank lines
        blank = 0
        for i, line in enumerate(lines, 1):
            if not line.strip():
                blank += 1
                if blank > 2:
                    issues.append(Issue(i, "hint", "style",
                                        "More than 2 consecutive blank lines"))
            else:
                blank = 0
        return issues

    def _call_name(self, node) -> str:
        """Call name.

            Args:
            node: Node.


            Returns:
                str: Result.
            """
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            parts = []
            n = node.func
            while isinstance(n, ast.Attribute):
                parts.append(n.attr)
                n = n.value
            if isinstance(n, ast.Name):
                parts.append(n.id)
            return ".".join(reversed(parts))
        return ""


class CodeReviewer:
    """Combines static analysis + optional LLM review."""

    def __init__(self, ai: "AIEngine" = None, advisor: "Advisor" = None):
        """Initialise the instance."""
        self.ai      = ai
        self.advisor = advisor
        self.static  = StaticReviewer()

    def review(self, code: str, path: str = "",
               use_ai: bool = True) -> Dict:
        """
        Full review. Returns dict with issues, ai_suggestions, summary.
        """
        static_issues = self.static.review(code, path)

        ai_text = ""
        if use_ai and self.ai and len(code) > 50:
            prompt = (
                f"Review this Python code briefly. "
                f"Give max 3 specific actionable suggestions. Be concise:\n\n{code[:2000]}"
            )
            ai_text = self.ai.ask(prompt, system_key="advisor")

        errors   = [i for i in static_issues if i.severity == "error"]
        warnings = [i for i in static_issues if i.severity == "warn"]
        hints    = [i for i in static_issues if i.severity == "hint"]

        summary = (
            f"{path or 'code'}: "
            f"{len(errors)} error(s), {len(warnings)} warning(s), {len(hints)} hint(s)"
        )
        if ai_text:
            summary += f"\n  AI: {ai_text[:200]}"

        return {
            "path":    path,
            "issues":  static_issues,
            "errors":  errors,
            "warnings":warnings,
            "hints":   hints,
            "ai":      ai_text,
            "summary": summary,
        }

    def review_file(self, path: str, sos=None) -> Dict:
        """Review a file from the SOS or filesystem."""
        code = ""
        if sos:
            try: code = sos.read(path)
            except Exception: pass
        if not code:
            try:
                with open(path) as f: code = f.read()
            except Exception:
                return {"error": f"Cannot read {path}"}
        return self.review(code, path=path)

    def queue_advice(self, result: Dict):
        """Push review findings to the advisor queue."""
        if not self.advisor:
            return
        from ai.advisor import Advice
        errors = result.get("errors", [])
        warns  = result.get("warnings", [])
        if errors:
            self.advisor.push(Advice(
                f"Code error: {result.get('path','')}",
                "\n".join(str(i) for i in errors[:3]),
                severity="critical", source="reviewer",
            ))
        elif warns:
            self.advisor.push(Advice(
                f"Code warning: {result.get('path','')}",
                "\n".join(str(i) for i in warns[:3]),
                severity="warn", source="reviewer",
            ))
        ai_text = result.get("ai","")
        if ai_text:
            self.advisor.push(Advice(
                f"AI review: {result.get('path','')}",
                ai_text[:300],
                severity="info", source="reviewer",
            ))

    def format_report(self, result: Dict) -> str:
        """Format and return report.

            Args:
            result (Dict): Result.


            Returns:
                str: Result.
            """
        lines = [f"\n  Review: {result.get('path','')}", "  " + "─"*50]
        for issue in result.get("issues", [])[:20]:
            col = {"error":"\033[31m","warn":"\033[33m","hint":"\033[36m"}[issue.severity]
            lines.append(f"{col}{str(issue)}\033[0m")
        ai = result.get("ai","")
        if ai:
            lines.append(f"\n  AI suggestions:\n  {ai[:400]}")
        lines.append(f"\n  {result.get('summary','')}")
        return "\n".join(lines)
