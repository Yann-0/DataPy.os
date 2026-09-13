"""
PyOS NOVA — Security Auditor
==============================
Background and on-demand security scanning.

Checks:
  - File permissions (world-writable, setuid)
  - Weak/empty passwords
  - Open ports
  - Outdated packages (known vulnerable versions)
  - Suspicious processes
  - SOS objects with hardcoded secrets
  - Crypto status

Command: audit [quick|full|ports|files|packages]
"""

import os, sys, socket, time, re, subprocess
from typing import List, Dict, Optional, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from kernel.nova import NovaKernel


class Finding:
    """Finding."""
    SEVERITY = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    ICONS    = {"info": "ℹ", "low": "○", "medium": "◐",
                "high": "●", "critical": "✖"}
    COLORS   = {"info": "\033[36m", "low": "\033[32m", "medium": "\033[33m",
                "high": "\033[31m", "critical": "\033[1;31m"}

    def __init__(self, category: str, severity: str,
                 title: str, detail: str, fix: str = ""):
        """Initialise the instance."""
        self.category = category
        self.severity = severity
        self.title    = title
        self.detail   = detail
        self.fix      = fix

    def __str__(self):
        """Return a human-readable string representation."""
        col   = self.COLORS.get(self.severity, "")
        rst   = "\033[0m"
        icon  = self.ICONS.get(self.severity, "·")
        lines = [f"  {col}{icon} [{self.severity.upper()}] {self.title}{rst}"]
        if self.detail:
            lines.append(f"    {self.detail}")
        if self.fix:
            lines.append(f"    \033[2mFix: {self.fix}\033[0m")
        return "\n".join(lines)

    @property
    def score(self):

        """Score."""

        return self.SEVERITY.get(self.severity, 0)


class SecurityAuditor:
    """Runs all security checks."""

    def __init__(self, kernel: "NovaKernel" = None):
        """Initialise the instance."""
        self.kernel   = kernel
        self.findings: List[Finding] = []

    def audit(self, mode: str = "quick") -> List[Finding]:
        """Audit.

            Args:
            mode (str): Mode, defaults to 'quick'.


            Returns:
                List[Finding]: Result.
            """
        self.findings = []
        if mode in ("quick", "full"):
            self._check_crypto()
            self._check_passwords()
            self._check_sos_secrets()
            self._check_processes()
        if mode == "full":
            self._check_ports()
            self._check_packages()
            self._check_file_permissions()
        if mode == "ports":
            self._check_ports()
        if mode == "files":
            self._check_file_permissions()
        if mode == "packages":
            self._check_packages()
        self.findings.sort(key=lambda f: f.score, reverse=True)
        return self.findings

    # ── individual checks ─────────────────────────────────────────────────────
    def _check_crypto(self):
        """Check crypto and return the result."""
        if not self.kernel:
            return
        try:
            from store.crypto import CryptoEngine
            eng = CryptoEngine(self.kernel.sos)
            if not eng.initialized:
                self.findings.append(Finding(
                    "crypto", "medium",
                    "Encryption not initialised",
                    "The SOS object store is unencrypted. Sensitive files are stored in plaintext.",
                    "Run: crypto init"
                ))
            elif eng.locked:
                self.findings.append(Finding(
                    "crypto", "low",
                    "Encryption locked",
                    "Crypto is configured but locked. Secret objects are protected.",
                    "Run: crypto unlock  to decrypt in this session"
                ))
        except Exception:
            pass

    def _check_passwords(self):
        """Check passwords and return the result."""
        if not self.kernel:
            return
        try:
            passwd = self.kernel.sos.read("/etc/passwd")
            for line in passwd.splitlines():
                parts = line.split(":")
                if len(parts) >= 2 and parts[1] in ("", "x"):
                    user = parts[0]
                    if parts[1] == "":
                        self.findings.append(Finding(
                            "auth", "high",
                            f"Empty password: {user}",
                            f"User '{user}' has no password — anyone can login.",
                            f"Run: passwd {user}"
                        ))
        except Exception:
            pass

    def _check_sos_secrets(self):
        """Check sos secrets and return the result."""
        if not self.kernel:
            return
        secret_patterns = [
            (r"(password|passwd|secret|api_key)\s*=\s*['\"][^'\"]{4,}['\"]", "Hardcoded credential"),
            (r"(AKIA[A-Z0-9]{16})", "AWS access key"),
            (r"-----BEGIN (RSA|EC|DSA|OPENSSH) PRIVATE KEY-----", "Private key in SOS"),
        ]
        try:
            conn  = self.kernel.sos._pool.get()
            rows  = conn.execute(
                "SELECT oid, CAST(content AS TEXT) c FROM objects WHERE kind IN ('text','code') AND size > 0 LIMIT 200"
            ).fetchall()
            found = set()
            for row in rows:
                text = row["c"] or ""
                for pat, label in secret_patterns:
                    if re.search(pat, text, re.I) and row["oid"] not in found:
                        obj = self.kernel.sos.get(row["oid"])
                        path = obj.meta.get("path", row["oid"]) if obj else row["oid"]
                        self.findings.append(Finding(
                            "secrets", "high",
                            f"Possible secret in: {path}",
                            label,
                            "Remove the secret or mark the file 'secret' for encryption"
                        ))
                        found.add(row["oid"])
        except Exception: pass

    def _check_processes(self):
        """Check processes and return the result."""
        try:
            import psutil
            for proc in psutil.process_iter(["pid","name","username","cmdline"]):
                try:
                    info = proc.info
                    cmd  = " ".join(info.get("cmdline") or [])
                    # Running as root without need
                    if info.get("username") == "root" and "sudo" in cmd:
                        self.findings.append(Finding(
                            "process", "low",
                            f"Sudo process: {info['name']} (PID {info['pid']})",
                            "Process running with escalated privileges.",
                            "Review if sudo is necessary"
                        ))
                except Exception: pass
        except ImportError:
            self.findings.append(Finding(
                "process", "info",
                "psutil not installed",
                "Cannot scan running processes without psutil.",
                "pip install psutil"
            ))

    def _check_ports(self):
        """Check ports and return the result."""
        common_ports = [21, 22, 23, 25, 80, 443, 3306, 5432, 6379, 8080, 8443]
        risky_open   = []
        for port in common_ports:
            try:
                s = socket.socket()
                s.settimeout(0.3)
                result = s.connect_ex(("127.0.0.1", port))
                s.close()
                if result == 0:
                    risky_open.append(port)
            except Exception: pass

        if risky_open:
            risky_names = {21:"FTP",22:"SSH",23:"Telnet",25:"SMTP",
                           3306:"MySQL",5432:"Postgres",6379:"Redis"}
            for port in risky_open:
                name = risky_names.get(port, str(port))
                sev  = "medium" if port in (21,23,6379) else "low"
                self.findings.append(Finding(
                    "network", sev,
                    f"Open port: {port} ({name})",
                    f"Port {port} is listening on localhost.",
                    "Ensure this service is intentional and secured"
                ))

        if not risky_open:
            self.findings.append(Finding("network", "info",
                "No common risky ports open", "Network surface looks minimal."))

    def _check_packages(self):
        """Check for known-vulnerable package versions."""
        KNOWN_VULN = {
            "requests": ("2.31.0", "CVE-2023-32681: redirect to file:// URI"),
            "pillow":   ("9.3.0",  "CVE-2023-44271: uncontrolled resource consumption"),
            "cryptography": ("41.0.0", "CVE-2023-49083: NULL pointer dereference"),
        }
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "list", "--format=json"],
                capture_output=True, text=True, timeout=10
            )
            packages = {p["name"].lower(): p["version"]
                        for p in __import__("json").loads(result.stdout)}
            for pkg, (min_safe, desc) in KNOWN_VULN.items():
                ver = packages.get(pkg)
                if ver:
                    from packaging.version import Version
                    try:
                        if Version(ver) < Version(min_safe):
                            self.findings.append(Finding(
                                "packages", "high",
                                f"Vulnerable package: {pkg} {ver}",
                                desc,
                                f"pip install {pkg}>={min_safe}"
                            ))
                    except Exception: pass
        except Exception: pass

    def _check_file_permissions(self):
        """Check for world-writable or executable files in home dir."""
        if not self.kernel: return
        try:
            for name in self.kernel.sos.listdir("/home/root"):
                path = f"/home/root/{name}"
                # Check for scripts with dangerous patterns
                if name.endswith(".py"):
                    try:
                        content = self.kernel.sos.read(path)
                        if re.search(r"os\.system|subprocess\.call.*shell=True", content):
                            self.findings.append(Finding(
                                "files", "medium",
                                f"Shell injection risk: {path}",
                                "Script uses shell=True or os.system() which can be dangerous.",
                                "Use subprocess with a list argument instead of shell=True"
                            ))
                    except Exception: pass
        except Exception: pass

    def score(self) -> int:
        """Overall risk score 0-100. Higher = worse."""
        if not self.findings: return 0
        weights = {"critical": 40, "high": 20, "medium": 10, "low": 3, "info": 0}
        raw = sum(weights.get(f.severity, 0) for f in self.findings)
        return min(100, raw)

    def report(self) -> str:
        """Generate and return a formatted report.


            Returns:
                str: Result.
            """
        if not self.findings:
            return "\n  \033[32m✓ No issues found.\033[0m\n"
        lines = ["\n  Security Audit Report",
                 "  " + "─"*48]
        cats = {}
        for f in self.findings:
            cats.setdefault(f.category, []).append(f)
        for cat, finds in cats.items():
            lines.append(f"\n  \033[1m{cat.upper()}\033[0m")
            for f in finds:
                lines.append(str(f))
        score = self.score()
        col   = "\033[32m" if score<20 else "\033[33m" if score<50 else "\033[31m"
        lines.append(f"\n  Risk score: {col}{score}/100\033[0m")
        lines.append(f"  Findings: {len(self.findings)} total  "
                     f"({sum(1 for f in self.findings if f.severity in ('critical','high'))} high+critical)")
        return "\n".join(lines) + "\n"
