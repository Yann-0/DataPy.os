"""
PyOS NOVA — SOS Schema Validation
====================================
Objects tagged with a schema URI are validated on every write.
Violations are quarantined (flagged + logged) rather than rejected,
preserving data while surfacing quality issues.

Schema evolution is tracked via SOS versioning: updating a schema
at /schemas/user.json creates a new version, and objects are
re-validated lazily on next access.

Uses a pure Python JSON Schema subset (no external library):
  Supported: type, properties, required, minLength, maxLength,
             minimum, maximum, pattern, enum, items, additionalProperties

Shell commands:
  schema define <n> <json>   — define a schema at /schemas/<n>.json
  schema validate <path>      — validate an object against its schema
  schema attach <path> <n>  — attach a schema to an object (adds tag)
  schema list                 — list all schemas
  schema violations           — show recent validation violations
"""

from __future__ import annotations
import os, sys, json, re, time, threading
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore

SCHEMA_BASE     = "/schemas"
VIOLATION_LOG   = "/schemas/.violations"
SCHEMA_TAG_PREFIX = "schema:"


class ValidationError:
    """A single schema validation failure."""

    def __init__(self, path: str, field: str, message: str):
        """Initialise a validation error."""
        self.path    = path
        self.field   = field
        self.message = message

    def __str__(self) -> str:
        """Return human-readable description."""
        return f"  [{self.field}] {self.message} (in {self.path})"


def _validate_type(value: Any, expected: str) -> bool:
    """Check JSON Schema type constraint."""
    type_map = {
        "string":  str,
        "number":  (int, float),
        "integer": int,
        "boolean": bool,
        "array":   list,
        "object":  dict,
        "null":    type(None),
    }
    t = type_map.get(expected)
    return isinstance(value, t) if t else True


def validate_against_schema(data: Any, schema: dict,
                              path: str = "#") -> List[ValidationError]:
    """
    Validate data against a JSON Schema subset.

    Args:
        data: The data to validate.
        schema (dict): JSON Schema (subset supported).
        path (str): JSON pointer path for error messages.

    Returns:
        List[ValidationError]: List of validation failures (empty = valid).
    """
    errors: List[ValidationError] = []

    if not isinstance(schema, dict):
        return errors

    # type check
    if "type" in schema:
        expected = schema["type"]
        if isinstance(expected, list):
            if not any(_validate_type(data, t) for t in expected):
                errors.append(ValidationError(path, "type",
                    f"Expected one of {expected}, got {type(data).__name__}"))
        elif not _validate_type(data, expected):
            errors.append(ValidationError(path, "type",
                f"Expected {expected}, got {type(data).__name__}"))
            return errors   # can't proceed with wrong type

    # string constraints
    if isinstance(data, str):
        if "minLength" in schema and len(data) < schema["minLength"]:
            errors.append(ValidationError(path, "minLength",
                f"Length {len(data)} < minimum {schema['minLength']}"))
        if "maxLength" in schema and len(data) > schema["maxLength"]:
            errors.append(ValidationError(path, "maxLength",
                f"Length {len(data)} > maximum {schema['maxLength']}"))
        if "pattern" in schema and not re.search(schema["pattern"], data):
            errors.append(ValidationError(path, "pattern",
                f"Does not match pattern {schema['pattern']!r}"))
        if "enum" in schema and data not in schema["enum"]:
            errors.append(ValidationError(path, "enum",
                f"Value not in allowed set {schema['enum']}"))

    # numeric constraints
    if isinstance(data, (int, float)):
        if "minimum" in schema and data < schema["minimum"]:
            errors.append(ValidationError(path, "minimum",
                f"{data} < minimum {schema['minimum']}"))
        if "maximum" in schema and data > schema["maximum"]:
            errors.append(ValidationError(path, "maximum",
                f"{data} > maximum {schema['maximum']}"))

    # object constraints
    if isinstance(data, dict):
        req = schema.get("required", [])
        for field in req:
            if field not in data:
                errors.append(ValidationError(path, "required",
                    f"Required field '{field}' is missing"))
        props = schema.get("properties", {})
        for key, val in data.items():
            if key in props:
                errors.extend(
                    validate_against_schema(val, props[key],
                                             f"{path}/{key}")
                )
        if schema.get("additionalProperties") is False:
            for key in data:
                if key not in props:
                    errors.append(ValidationError(path, "additionalProperties",
                        f"Unexpected property '{key}'"))

    # array constraints
    if isinstance(data, list):
        if "items" in schema:
            item_schema = schema["items"]
            for i, item in enumerate(data):
                errors.extend(
                    validate_against_schema(item, item_schema,
                                             f"{path}/{i}")
                )
        if "minItems" in schema and len(data) < schema["minItems"]:
            errors.append(ValidationError(path, "minItems",
                f"Array length {len(data)} < minimum {schema['minItems']}"))
        if "maxItems" in schema and len(data) > schema["maxItems"]:
            errors.append(ValidationError(path, "maxItems",
                f"Array length {len(data)} > maximum {schema['maxItems']}"))

    return errors


class SchemaRegistry:
    """
    Manages JSON Schemas for SOS object validation.

    Schemas are stored in the SOS at /schemas/<name>.json.
    Objects are linked to schemas via tags: schema:/schemas/user.json
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the schema registry."""
        self.sos         = sos
        self._cache:     Dict[str, dict] = {}
        self._violations: List[dict] = []
        self._lock       = threading.Lock()
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create schema storage directory."""
        if not self.sos.exists(SCHEMA_BASE):
            self.sos.mkdir(SCHEMA_BASE, parents=True)

    def define(self, name: str, schema: dict) -> str:
        """
        Define or update a schema.

        Args:
            name (str): Schema name (without .json extension).
            schema (dict): JSON Schema definition.

        Returns:
            str: SOS path of the stored schema.
        """
        path = f"{SCHEMA_BASE}/{name}.json"
        self.sos.write(path, json.dumps(schema, indent=2),
                        kind="data", tags=["schema"])
        with self._lock:
            self._cache[path] = schema
        return path

    def get(self, schema_uri: str) -> Optional[dict]:
        """
        Retrieve a schema by URI (SOS path).

        Args:
            schema_uri (str): Schema path (e.g. /schemas/user.json).

        Returns:
            dict: The JSON Schema, or None if not found.
        """
        with self._lock:
            if schema_uri in self._cache:
                return self._cache[schema_uri]
        try:
            schema = json.loads(self.sos.read(schema_uri))
            with self._lock:
                self._cache[schema_uri] = schema
            return schema
        except Exception:
            return None

    def _schema_uri_for(self, path: str) -> Optional[str]:
        """Return the schema URI for a path, from its tags."""
        tags = self.sos.get_tags(path)
        for tag in tags:
            if tag.startswith(SCHEMA_TAG_PREFIX):
                return tag[len(SCHEMA_TAG_PREFIX):]
        return None

    def validate(self, path: str) -> Tuple[bool, List[ValidationError]]:
        """
        Validate an SOS object against its attached schema.

        Args:
            path (str): SOS path to validate.

        Returns:
            Tuple[bool, List[ValidationError]]: (is_valid, errors)
        """
        schema_uri = self._schema_uri_for(path)
        if not schema_uri:
            return True, []   # no schema attached

        schema = self.get(schema_uri)
        if not schema:
            return True, []   # schema not found — skip

        try:
            content = self.sos.read(path)
            data    = json.loads(content)
        except json.JSONDecodeError:
            # Non-JSON content — only validate as string
            data = self.sos.read(path)
        except Exception:
            return True, []

        errors = validate_against_schema(data, schema)
        if errors:
            self._record_violation(path, schema_uri, errors)
        return len(errors) == 0, errors

    def _record_violation(self, path: str, schema_uri: str,
                           errors: List[ValidationError]):
        """Log a validation violation."""
        entry = {
            "ts":         time.time(),
            "path":       path,
            "schema_uri": schema_uri,
            "errors":     [str(e) for e in errors[:5]],
        }
        with self._lock:
            self._violations.append(entry)
            if len(self._violations) > 500:
                self._violations.pop(0)
        # Quarantine flag
        try:
            self.sos.tag(path, "schema-violation")
        except Exception:
            pass

    def attach(self, path: str, schema_name: str):
        """
        Attach a schema to an SOS object via tag.

        Args:
            path (str): SOS path to attach schema to.
            schema_name (str): Schema name (looks up /schemas/<name>.json).
        """
        schema_uri = f"{SCHEMA_BASE}/{schema_name}.json"
        self.sos.tag(path, f"{SCHEMA_TAG_PREFIX}{schema_uri}")

    def violations(self, n: int = 50) -> List[dict]:
        """Return recent validation violations."""
        with self._lock:
            return list(reversed(self._violations[-n:]))

    def list_schemas(self) -> List[str]:
        """Return all defined schema names."""
        return [n for n in self.sos.listdir(SCHEMA_BASE)
                if n.endswith(".json")]

    def patch_sos(self):
        """Patch SOS to auto-validate objects on write."""
        orig_write = self.sos.write
        registry   = self

        def _validated_write(path: str, content, **kw):
            oid = orig_write(path, content, **kw)
            # Async validation (don't block the write)
            threading.Thread(
                target=lambda: registry.validate(path),
                daemon=True
            ).start()
            return oid

        self.sos.write = _validated_write
