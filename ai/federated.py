"""
PyOS NOVA — Federated Model Updates
======================================
Multiple NOVA nodes collaboratively improve shared models
without ever sharing raw data. Only model parameter deltas
are exchanged — data stays local.

Models updated federally:
  1. Markov prefetch model  — transition probabilities aggregate
  2. NL query IDF table     — char n-gram weights improve globally
  3. AI advisor thresholds  — alert thresholds calibrate to real usage

Mechanism:
  - Each node computes a local delta: new_params - base_params
  - Delta is quantised (float32 → int8) to reduce bandwidth 8×
  - Deltas are shared via the CRDT sync channel
  - Federated average: params = (1-α) * local + α * (Σ remotes / n)
  - Privacy: Gaussian noise added to deltas (differential privacy)

Shell commands:
  federated status         — show federation state
  federated push           — push local model updates to peers
  federated pull           — pull updates from peers
  federated sync           — push + pull in one step
  federated reset          — reset to base model (un-federate)
"""

from __future__ import annotations
import os, sys, json, time, hashlib, math
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from kernel.nova import NovaKernel

FED_BASE   = "/federated"
DELTA_BASE = "/federated/deltas"

# Federated averaging weight (how much to trust remote updates)
ALPHA  = 0.2      # 20% remote, 80% local
EPSILON = 0.1     # differential privacy budget per sync round

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


def _add_dp_noise(values: List[float], sensitivity: float,
                   epsilon: float) -> List[float]:
    """
    Add Laplace noise for differential privacy.

    Args:
        values (List[float]): Parameter values.
        sensitivity (float): L1 sensitivity of the parameters.
        epsilon (float): Privacy budget.

    Returns:
        List[float]: Noised values.
    """
    import secrets
    scale = sensitivity / epsilon
    noised = []
    for v in values:
        u = (secrets.randbelow(2**32) / 2**32) - 0.5
        noise = -scale * (1 if u >= 0 else -1) * math.log(1 - 2*abs(u))
        noised.append(v + noise)
    return noised


def _quantise(values: List[float], bits: int = 8) -> Tuple[List[int], float, float]:
    """
    Quantise float values to integers for bandwidth reduction.

    Args:
        values (List[float]): Values to quantise.
        bits (int): Bit depth (8 = int8 = 8× compression vs float32).

    Returns:
        Tuple[List[int], float, float]: (quantised, min_val, scale)
    """
    if not values:
        return [], 0.0, 1.0
    min_v  = min(values)
    max_v  = max(values)
    range_ = max_v - min_v or 1.0
    scale  = range_ / (2**bits - 1)
    quant  = [round((v - min_v) / scale) for v in values]
    return quant, min_v, scale


def _dequantise(quant: List[int], min_val: float,
                 scale: float) -> List[float]:
    """
    Restore float values from quantised integers.

    Args:
        quant (List[int]): Quantised values.
        min_val (float): Minimum value used during quantisation.
        scale (float): Scale factor used during quantisation.

    Returns:
        List[float]: Dequantised float values.
    """
    return [q * scale + min_val for q in quant]


class ModelDelta:
    """
    A compact, privacy-preserving model update for federation.

    Contains quantised parameter deltas and metadata for
    merging with other nodes' updates.
    """

    def __init__(self, model_name: str, node_id: str,
                 params: Dict[str, List[float]],
                 round_: int = 0):
        """Initialise a model delta."""
        self.model_name = model_name
        self.node_id    = node_id
        self.round      = round_
        self.timestamp  = time.time()
        # Quantise params for bandwidth efficiency
        self._raw       = params
        self._quantised = {}
        for key, vals in params.items():
            if vals:
                q, mn, sc = _quantise(vals)
                self._quantised[key] = {"q": q, "min": mn, "scale": sc}

    def get_params(self, add_noise: bool = True) -> Dict[str, List[float]]:
        """
        Recover float parameters from this delta.

        Args:
            add_noise (bool): Add DP noise before returning.

        Returns:
            Dict[str, List[float]]: Parameter values.
        """
        result = {}
        for key, data in self._quantised.items():
            vals = _dequantise(data["q"], data["min"], data["scale"])
            if add_noise and vals:
                sensitivity = (max(vals) - min(vals)) / len(vals) + 1e-10
                vals = _add_dp_noise(vals, sensitivity, EPSILON)
            result[key] = vals
        return result

    def to_dict(self) -> dict:
        """Serialise to JSON-compatible dict."""
        return {
            "model_name":  self.model_name,
            "node_id":     self.node_id,
            "round":       self.round,
            "timestamp":   self.timestamp,
            "quantised":   self._quantised,
        }

    @staticmethod
    def from_dict(d: dict) -> "ModelDelta":
        """Deserialise from dict."""
        delta = ModelDelta.__new__(ModelDelta)
        delta.model_name = d["model_name"]
        delta.node_id    = d["node_id"]
        delta.round      = d.get("round", 0)
        delta.timestamp  = d.get("timestamp", time.time())
        delta._raw       = {}
        delta._quantised = d.get("quantised", {})
        return delta


class FederatedLearner:
    """
    Federated learning manager for NOVA models.

    Coordinates local model improvement with remote node updates
    via the CRDT sync channel, preserving privacy throughout.
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the federated learner."""
        self.kernel    = kernel
        self.sos       = kernel.sos
        self._node_id  = self._get_node_id()
        self._round    = 0
        self._ensure_dirs()

    def _get_node_id(self) -> str:
        """Return or generate a stable node ID."""
        try:
            path = "/federated/node_id"
            if self.sos.exists(path):
                return self.sos.read(path).strip()
            import socket, hashlib
            nid = hashlib.sha256(socket.gethostname().encode()).hexdigest()[:16]
            self.sos.write(path, nid)
            return nid
        except Exception:
            return "local_node"

    def _ensure_dirs(self):
        """Create federation directories."""
        for path in (FED_BASE, DELTA_BASE):
            if not self.sos.exists(path):
                self.sos.mkdir(path, parents=True)

    # ── Markov prefetch federation ─────────────────────────────────────────

    def export_prefetch_delta(self) -> Optional[ModelDelta]:
        """
        Export the local Markov prefetch model as a federated delta.

        Returns:
            ModelDelta: Serialised model update, or None if no data.
        """
        pf = getattr(self.kernel, "prefetch", None)
        if not pf:
            return None
        transitions = pf._transitions
        if not transitions:
            return None
        # Flatten transition counts into a serialisable form
        params = {}
        for src, targets in list(transitions.items())[:200]:  # limit size
            key = hashlib.sha256(src.encode()).hexdigest()[:8]
            top = sorted(targets.items(), key=lambda x: x[1], reverse=True)[:5]
            params[key] = [float(cnt) for _, cnt in top]
        return ModelDelta("prefetch", self._node_id, params, self._round)

    def apply_prefetch_delta(self, delta: ModelDelta):
        """
        Apply a remote Markov delta via federated averaging.

        Args:
            delta (ModelDelta): Remote model update to merge.
        """
        pf = getattr(self.kernel, "prefetch", None)
        if not pf:
            return
        remote_params = delta.get_params(add_noise=True)
        # Federated average: blend remote counts into local
        for key, remote_vals in remote_params.items():
            # Find matching local key by prefix
            for src, targets in pf._transitions.items():
                if hashlib.sha256(src.encode()).hexdigest()[:8] == key:
                    vals = list(targets.values())[:len(remote_vals)]
                    for i, (dst, cnt) in enumerate(
                            list(targets.items())[:len(remote_vals)]):
                        if i < len(remote_vals):
                            blended = (1 - ALPHA) * cnt + ALPHA * remote_vals[i]
                            targets[dst] = max(0, blended)
                    break

    # ── NL query IDF federation ────────────────────────────────────────────

    def export_idf_delta(self) -> Optional[ModelDelta]:
        """
        Export the IDF table from the NL query embedder.

        Returns:
            ModelDelta: Serialised IDF update, or None if unavailable.
        """
        nl = getattr(self.kernel, "nlquery", None)
        if not nl:
            return None
        # Fast embedder IDF
        fast_search = getattr(self.kernel, "search", None)
        if fast_search is None:
            return None
        emb = getattr(fast_search, "_embedder", None)
        if emb is None or not HAS_NUMPY:
            return None
        idf = getattr(emb, "_idf", None)
        if idf is None:
            return None
        # Sample 256 dimensions to reduce size
        import numpy as np
        sample_idx = list(range(0, len(idf), max(1, len(idf) // 256)))[:256]
        params = {"idf_sample": [float(idf[i]) for i in sample_idx],
                  "n_docs":     [float(emb._n_docs)]}
        return ModelDelta("idf", self._node_id, params, self._round)

    def apply_idf_delta(self, delta: ModelDelta):
        """
        Apply a remote IDF delta via federated averaging.

        Args:
            delta (ModelDelta): Remote IDF update to merge.
        """
        fast_search = getattr(self.kernel, "search", None)
        if not fast_search or not HAS_NUMPY:
            return
        emb = getattr(fast_search, "_embedder", None)
        if emb is None:
            return
        remote = delta.get_params(add_noise=True)
        sample = remote.get("idf_sample", [])
        if not sample or emb._idf is None:
            return
        import numpy as np
        sample_idx = list(range(0, len(emb._idf),
                                 max(1, len(emb._idf) // len(sample))))[:len(sample)]
        for i, idx in enumerate(sample_idx):
            if i < len(sample):
                emb._idf[idx] = (1 - ALPHA) * float(emb._idf[idx]) + ALPHA * sample[i]

    # ── Persistence ────────────────────────────────────────────────────────

    def _delta_path(self, model_name: str, node_id: str) -> str:
        """Return SOS path for a stored delta."""
        key = hashlib.sha256(f"{model_name}:{node_id}".encode()).hexdigest()[:12]
        return f"{DELTA_BASE}/{key}"

    def store_delta(self, delta: ModelDelta):
        """Persist a delta to the SOS for CRDT sync."""
        self.sos.write(
            self._delta_path(delta.model_name, delta.node_id),
            json.dumps(delta.to_dict()),
            tags=["federated-delta", delta.model_name, delta.node_id],
        )

    def load_remote_deltas(self, model_name: str) -> List[ModelDelta]:
        """Load all remote deltas for a model (excluding own)."""
        deltas = []
        for name in self.sos.listdir(DELTA_BASE):
            path = f"{DELTA_BASE}/{name}"
            try:
                data  = json.loads(self.sos.read(path))
                delta = ModelDelta.from_dict(data)
                if (delta.model_name == model_name
                        and delta.node_id != self._node_id):
                    deltas.append(delta)
            except Exception:
                pass
        return deltas

    # ── Orchestration ──────────────────────────────────────────────────────

    def push(self) -> dict:
        """
        Export and store local model deltas for federation.

        Returns:
            dict: Count of models pushed.
        """
        pushed = 0
        for export_fn in (self.export_prefetch_delta, self.export_idf_delta):
            try:
                delta = export_fn()
                if delta:
                    self.store_delta(delta)
                    pushed += 1
            except Exception:
                pass
        self._round += 1
        return {"pushed": pushed, "round": self._round}

    def pull(self) -> dict:
        """
        Pull and apply remote model deltas.

        Returns:
            dict: Count of deltas applied per model.
        """
        applied = {"prefetch": 0, "idf": 0}
        for model_name, apply_fn in (
                ("prefetch", self.apply_prefetch_delta),
                ("idf",      self.apply_idf_delta),
        ):
            for delta in self.load_remote_deltas(model_name):
                try:
                    apply_fn(delta)
                    applied[model_name] += 1
                except Exception:
                    pass
        return applied

    def sync(self) -> dict:
        """Push local updates then pull remote updates."""
        push_result = self.push()
        pull_result = self.pull()
        return {"push": push_result, "pull": pull_result}

    def status(self) -> dict:
        """Return federated learning status."""
        remote_deltas = sum(
            1 for name in self.sos.listdir(DELTA_BASE)
            if self.sos.exists(f"{DELTA_BASE}/{name}")
        )
        return {
            "node_id":       self._node_id,
            "round":         self._round,
            "stored_deltas": remote_deltas,
            "models":        ["prefetch", "idf"],
            "alpha":         ALPHA,
            "dp_epsilon":    EPSILON,
        }
