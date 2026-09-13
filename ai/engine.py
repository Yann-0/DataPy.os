"""
PyOS NOVA — AI Engine v2 (GPU-aware)
======================================
Three-tier local inference engine.  No Ollama, no cloud API required.

Tier selection (automatic, in priority order):

    Tier 1 — ``llama-cpp-python``
        Loads any GGUF model file directly into memory.  GPU layers are
        offloaded automatically based on available VRAM (detected by
        :mod:`ai.gpu`).  Best quality; requires a model download.

    Tier 2 — ``NanoLLM``
        GPT-2 autoregressive transformer implemented entirely in numpy.
        Slower than Tier 1 but requires only numpy and pre-downloaded
        GPT-2 weights.  Appropriate for constrained environments.

    Tier 3 — ``RAGEngine``
        Keyword retrieval over a curated OS knowledge base.  Zero
        dependencies beyond the standard library.  Always available.
        Used when no model is installed.

Usage::

    engine = AIEngine()
    # Check which tier is active
    print(engine.tier)          # "rag" | "nano" | "llama-cpp"
    # Stream a response
    for token in engine.complete("explain the SOS"):
        print(token, end="", flush=True)
    # One-shot
    answer = engine.ask("what is PyOS NOVA?")

GPU acceleration (after ``pip install llama-cpp-python``):
    CUDA:   ``CMAKE_ARGS="-DLLAMA_CUDA=on"   pip install llama-cpp-python --force-reinstall``
    ROCm:   ``CMAKE_ARGS="-DLLAMA_HIPBLAS=on" pip install llama-cpp-python --force-reinstall``
    Metal:  ``CMAKE_ARGS="-DLLAMA_METAL=on"   pip install llama-cpp-python --force-reinstall``
======================================
Tier 1: llama-cpp-python with GPU (CUDA/ROCm/Metal/Vulkan auto-detected)
Tier 2: NanoLLM — GPT-2 in pure numpy
Tier 3: RAG knowledge base — always works, zero deps
"""

import os, re, json, time, threading
from typing import Iterator, Optional, List

DATA_DIR = os.environ.get("NOVA_DATA", os.path.expanduser("~/.nova"))


class LlamaCppBackend:
    """Llama cpp backend."""
    DEFAULT_MODELS = {
        "tinyllama": "https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF/resolve/main/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
        "phi3":      "https://huggingface.co/microsoft/Phi-3-mini-4k-instruct-gguf/resolve/main/Phi-3-mini-4k-instruct-q4.gguf",
        "mistral":   "https://huggingface.co/TheBloke/Mistral-7B-Instruct-v0.2-GGUF/resolve/main/mistral-7b-instruct-v0.2.Q4_K_M.gguf",
    }

    def __init__(self, model_path=None):
        """Initialise the instance."""
        self.model_path = model_path
        self._llm       = None
        self._gpu_info  = None
        self._lock      = threading.Lock()

    def _load(self):
        """Load the operation."""
        try:
            from llama_cpp import Llama
        except ImportError:
            return False
        path = self.model_path or self._find_model()
        if not path:
            return False
        try:
            from ai.gpu import detect_gpu
            self._gpu_info = detect_gpu()
            n_gpu = self._gpu_info.n_gpu_layers
        except Exception:
            n_gpu = 0
        try:
            self._llm = Llama(model_path=path, n_ctx=4096,
                               n_threads=os.cpu_count() or 4,
                               n_gpu_layers=n_gpu, verbose=False)
            return True
        except Exception:
            return False

    def _find_model(self):
        """Find and return model."""
        for d in [os.path.join(DATA_DIR,"models"), os.path.expanduser("~/.nova/models"), "/data/nova/models"]:
            if not os.path.isdir(d): continue
            for f in sorted(os.listdir(d)):
                if f.endswith(".gguf"): return os.path.join(d, f)
        return None

    @property
    def available(self):
        """Available."""
        if self._llm is None: self._load()
        return self._llm is not None

    def complete(self, prompt, system="", max_tokens=512, temperature=0.7):
        """Complete.

            Args:
            prompt: Prompt.
            system: System, defaults to ''.
            max_tokens: Max tokens, defaults to 512.
            temperature: Temperature, defaults to 0.7.
            """
        if not self.available: return
        messages = ([{"role":"system","content":system}] if system else []) + [{"role":"user","content":prompt}]
        with self._lock:
            try:
                for chunk in self._llm.create_chat_completion(messages=messages,
                    max_tokens=max_tokens, temperature=temperature, stream=True):
                    delta = chunk["choices"][0]["delta"].get("content","")
                    if delta: yield delta
            except Exception as e:
                yield f"\n[llama-cpp: {e}]"

    def download(self, name="tinyllama"):
        """Download the operation to local storage.

            Args:
            name: Name, defaults to 'tinyllama'.
            """
        import urllib.request
        url  = self.DEFAULT_MODELS.get(name, self.DEFAULT_MODELS["tinyllama"])
        dest = os.path.join(DATA_DIR, "models", f"{name}.gguf")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest): return dest
        print(f"  Downloading {name}...")
        def progress(b, bs, total):
            """Progress.

                Args:
                b: B.
                bs: Bs.
                total: Total.
                """
            if total > 0: print(f"\r  {min(b*bs/total*100,100):.0f}%  ", end="", flush=True)
        urllib.request.urlretrieve(url, dest, reporthook=progress)
        print(f"\r  Done — {dest}")
        return dest


class NanoLLM:
    """GPT-2 in pure numpy."""
    def __init__(self):
        """Initialise the instance."""
        self._ready = False; self._np = None

    def load(self):
        """Load the operation."""
        try:
            import numpy as np; self._np = np
            self._ready = self._load_weights(); return self._ready
        except ImportError: return False

    def _load_weights(self):
        """Load weights."""
        d = os.path.join(DATA_DIR, "models", "gpt2")
        if not all(os.path.exists(os.path.join(d,f)) for f in ["params.npz","encoder.json","merges.txt"]):
            return False
        try:
            data = self._np.load(os.path.join(d,"params.npz"), allow_pickle=True)
            self._wte = data["wte"]; self._wpe = data["wpe"]
            self._ln_f = (data["ln_f_g"], data["ln_f_b"])
            n = int(data.get("n_layers",[12])[0])
            self._blocks = [{"ln1_g":data[f"h{i}_ln1_g"],"ln1_b":data[f"h{i}_ln1_b"],
                              "ln2_g":data[f"h{i}_ln2_g"],"ln2_b":data[f"h{i}_ln2_b"],
                              "attn_c_attn_w":data[f"h{i}_attn_c_attn_w"],"attn_c_attn_b":data[f"h{i}_attn_c_attn_b"],
                              "attn_c_proj_w":data[f"h{i}_attn_c_proj_w"],"attn_c_proj_b":data[f"h{i}_attn_c_proj_b"],
                              "mlp_c_fc_w":data[f"h{i}_mlp_c_fc_w"],"mlp_c_fc_b":data[f"h{i}_mlp_c_fc_b"],
                              "mlp_c_proj_w":data[f"h{i}_mlp_c_proj_w"],"mlp_c_proj_b":data[f"h{i}_mlp_c_proj_b"]}
                             for i in range(n)]
            self._nh = int(data.get("n_head",[12])[0])
            with open(os.path.join(d,"encoder.json")) as f: enc = json.load(f)
            with open(os.path.join(d,"merges.txt"))   as f: self._merges = f.read().splitlines()
            self._id2tok = {v:k for k,v in enc.items()}; self._tok2id = enc
            return True
        except Exception: return False

    def generate(self, prompt, max_new=200, temperature=0.8, top_k=50):
        """Generate.

            Args:
            prompt: Prompt.
            max_new: Max new, defaults to 200.
            temperature: Temperature, defaults to 0.8.
            top_k: Top k, defaults to 50.
            """
        if not self._ready: return
        np = self._np
        toks = self._enc(prompt)
        if not toks: return
        for _ in range(max_new):
            ctx = np.array(toks[-512:]); pos = np.arange(len(ctx))
            x   = self._wte[ctx] + self._wpe[pos]
            for b in self._blocks:
                x = x + self._attn(self._ln(x, b["ln1_g"], b["ln1_b"]), b)
                x = x + self._mlp(self._ln(x, b["ln2_g"], b["ln2_b"]), b)
            x = self._ln(x, *self._ln_f)
            logits = (x @ self._wte.T)[-1]
            if temperature > 0: logits /= temperature
            ki  = np.argpartition(logits, -top_k)[-top_k:]
            pr  = np.exp(logits[ki]-logits[ki].max()); pr /= pr.sum()
            ch  = np.random.choice(ki, p=pr)
            toks.append(int(ch))
            word = self._id2tok.get(int(ch),"")
            yield word.replace("Ġ"," ").replace("Ċ","\n")
            if word in ("<|endoftext|>","\n\n"): break

    def _ln(self,x,g,b,e=1e-5):
        """Ln.

            Args:
            x: X.
            g: G.
            b: B.
            e: E, defaults to 1e-05.
            """
        np=self._np; m=x.mean(-1,keepdims=True); v=((x-m)**2).mean(-1,keepdims=True)
        return g*(x-m)/(v+e)**.5+b
    """Softmax.

        Args:
        x: X.
        """
    def _softmax(self,x): np=self._np; e=np.exp(x-x.max()); return e/e.sum()
    def _attn(self,x,b):
        """Attn.

            Args:
            x: X.
            b: B.
            """
        np=self._np; n,d=x.shape
        qkv=x@b["attn_c_attn_w"]+b["attn_c_attn_b"]; q,k,v=np.split(qkv,3,-1)
        nh=self._nh; dh=d//nh
        """Sh.

            Args:
            t: T.
            """
        def sh(t): return t.reshape(n,nh,dh).transpose(1,0,2)
        q,k,v=sh(q),sh(k),sh(v); w=q@k.transpose(0,2,1)/dh**.5
        w+=np.triu(np.full((n,n),-1e10),1)
        w=self._softmax(w.reshape(-1,n)).reshape(nh,n,n)
        return (w@v).transpose(1,0,2).reshape(n,d)@b["attn_c_proj_w"]+b["attn_c_proj_b"]
    def _mlp(self,x,b):
        """Mlp.

            Args:
            x: X.
            b: B.
            """
        np=self._np
        """G.

            Args:
            x: X.
            """
        def g(x): return .5*x*(1+np.tanh(.7978845608*(x+.044715*x**3)))
        return g(x@b["mlp_c_fc_w"]+b["mlp_c_fc_b"])@b["mlp_c_proj_w"]+b["mlp_c_proj_b"]
    def _enc(self, text):
        """Enc.

            Args:
            text: Text.
            """
        text=text.replace(" ","Ġ"); toks=list(text.encode("utf-8"))
        for merge in self._merges[:5000]:
            p=merge.split()
            if len(p)!=2: continue
            a,b=p; i,out=0,[]
            while i<len(toks):
                if i<len(toks)-1 and toks[i]==a and toks[i+1]==b: out.append(a+b); i+=2
                else: out.append(toks[i]); i+=1
            toks=out
        return [self._tok2id[t] for t in toks if t in self._tok2id]


OS_KB = {
    "pyos nova":        "PyOS NOVA is a Python OS where Python is PID 1. The kernel is just a hardware abstraction layer. The SOS replaces the filesystem with a versioned content-addressed object graph.",
    "semantic store":   "The Semantic Object Store (SOS) stores objects by SHA-256 hash. Every write creates a new version automatically. Objects are graph-linked and searchable by meaning.",
    "install package":  "Use `pip install <package>` to install any Python library. Real pip — available immediately. Example: `pip install flask numpy pandas`",
    "search files":     "Use `search <query>` for vector+BM25 search. `findtag <tag>` finds by tag. `sos history <path>` shows version history.",
    "write script":     "Use `write <description>` to have the AI generate a Python script. Add `--run` to execute it immediately.",
    "gpu support":      "GPU is auto-detected (CUDA/ROCm/Metal/Vulkan). In VirtualBox, CPU inference is used. Install a model: `llm download tinyllama`",
    "virtualbox":       "Import nova_vbox.ova into VirtualBox. The VM boots into NOVA directly (EFI → Python PID 1). 2GB RAM recommended.",
    "llm model":        "Use `llm download tinyllama` (637MB) or `llm download phi3` (2.3GB). Any GGUF file in ~/.nova/models/ is auto-detected.",
    "version history":  "Every file write is auto-versioned. `sos history <path>` lists versions. `sos checkout <path> <N>` retrieves version N.",
    "advisor":          "Background advisor checks disk, RAM, CPU every 5 min. LLM deep analysis every 30 min. Read with `advice`.",
    "python repl":      "Type `python3` for interactive REPL with `kernel` and `sos` available. Run files with `run <file>`.",
    "tag":              "Tag objects: `tag <path> <tag1> <tag2>`. Find by tag: `findtag <tag>`. All tags: `tags`.",
    "sos commands":     "`sos stats` — usage. `sos info <p>` — metadata. `sos history <p>` — versions. `sos relate <p1> <p2>` — link objects.",
    "chat":             "`chat` for interactive AI conversation. `ask <question>` for quick answer. `agent <task>` for AI-executed OS operations.",
    "reboot":           "`reboot` to reboot, `halt` to shut down. Python PID 1 handles clean shutdown.",
}

class RAGEngine:
    """R a g engine."""
    def complete(self, prompt, system="", **kw):
        """Complete.

            Args:
            prompt: Prompt.
            system: System, defaults to ''.
            """
        yield self._answer(prompt)
    def _answer(self, q):
        """Answer.

            Args:
            q: Q.
            """
        ql = q.lower()
        best, ans = 0, None
        for key, text in OS_KB.items():
            kw = set(key.split())
            s  = sum(1 for w in kw if w in ql) / max(len(kw),1)
            if s > best: best, ans = s, text
        if ans and best > 0.2: return ans
        for key, text in OS_KB.items():
            tw = set(text.lower().split())
            s  = sum(1 for w in ql.split() if w in tw) / max(len(ql.split()),1)
            if s > best: best, ans = s, text
        if ans and best > 0.08: return ans
        return "I can help with NOVA commands and Python scripting. Type `help` for all commands, or `agent <task>` to let the AI execute OS operations. Install a model with `llm download tinyllama` for full AI."


SYSTEMS = {
    "assistant": "You are PyOS NOVA AI. Help manage this Python OS. Be concise and technical.",
    "agent":     ('You are NOVA Agent. For OS tasks respond with JSON: {"action":"run_command","command":"...","reason":"..."} or {"action":"install_package","package":"...","reason":"..."} or {"action":"create_file","path":"...","content":"...","reason":"..."} or {"action":"none","reason":"..."}. Always confirm before destructive actions.'),
    "writer":    "Write clean Python 3.11+ code. Return ONLY code, no markdown fences.",
    "advisor":   "Analyze system metrics. Give 1-2 concise actionable recommendations.",
}


# ── AI Engine — Tiered LLM ────────────────────────────────────────────────────
# Automatically selects the best available inference tier:
#
#   Tier 1  llama-cpp-python  — GGUF model, GPU or CPU, full quality
#   Tier 2  NanoLLM           — tiny embedding-based responses, fast
#   Tier 3  RAG               — retrieval-augmented from SOS index
#
# The tier is detected at startup and can be overridden with NOVA_NO_AI=1.
# All tiers expose the same ask(prompt) API so callers don't need to
# know which tier is active.
#
class AIEngine:
    """A i engine."""
    def __init__(self, model_path=None):
        """Initialise the instance."""
        self._llama = LlamaCppBackend(model_path)
        self._nano  = NanoLLM()
        self._rag   = RAGEngine()
        self._tier  = None
        self._gpu   = None
        threading.Thread(target=self._detect, daemon=True).start()

    def _detect(self):
        """Detect the operation and return the result."""
        try:
            from ai.gpu import detect_gpu
            self._gpu = detect_gpu()
        except Exception: pass
        if self._llama.available: self._tier = "llama-cpp"
        elif self._nano.load():   self._tier = "nano"
        else:                     self._tier = "rag"

    @property
    def tier(self):
        """Tier."""
        return self._tier or "rag"

    @property
    def model_name(self):
        """Model name."""
        gpu_tag = ""
        if self._gpu and self._gpu.backend != "cpu" and self.tier == "llama-cpp":
            gpu_tag = f" [{self._gpu.backend.upper()}×{self._gpu.n_gpu_layers}L]"
        return {"llama-cpp":f"llama-cpp{gpu_tag}","nano":"NanoLLM(numpy)","rag":"RAG"}.get(self.tier,"init...")

    def complete(self, prompt, system_key="assistant", temperature=0.7, max_tokens=512, **kw):
        """Complete.

            Args:
            prompt: Prompt.
            system_key: System key, defaults to 'assistant'.
            temperature: Temperature, defaults to 0.7.
            max_tokens: Max tokens, defaults to 512.
            """
        system = SYSTEMS.get(system_key, SYSTEMS["assistant"])
        tier   = self.tier
        if tier == "llama-cpp":
            yield from self._llama.complete(prompt, system=system, max_tokens=max_tokens, temperature=temperature)
        elif tier == "nano":
            yield from self._nano.generate(f"System: {system}\nUser: {prompt}\nAssistant:", max_new=max_tokens, temperature=temperature)
        else:
            yield from self._rag.complete(prompt, system=system)

    def chat(self, messages, system_key="assistant", **kw):
        """Chat.

            Args:
            messages: Messages.
            system_key: System key, defaults to 'assistant'.
            """
        prompt = "\n".join(f"{'User' if m['role']=='user' else 'AI'}: {m['content']}" for m in messages)
        yield from self.complete(prompt, system_key=system_key, **kw)

    """Ask.

        Args:
        prompt: Prompt.
        """
    def ask(self, prompt, **kw): return "".join(self.complete(prompt, **kw))

    def download_model(self, name="tinyllama"):
        """Download model to local storage.

            Args:
            name: Name, defaults to 'tinyllama'.
            """
        path = self._llama.download(name)
        self._llama.model_path = path; self._llama._llm = None
        if self._llama.available: self._tier = "llama-cpp"; return True
        return False

    def status(self):
        """Return the current status as a dict."""
        d = {"tier": self.tier, "model": self.model_name, "llama_cpp": self._llama._llm is not None, "nano": self._nano._ready}
        if self._gpu:
            d.update({"gpu_backend": self._gpu.backend, "gpu_name": self._gpu.name,
                       "gpu_vram": f"{self._gpu.vram_gb}GB", "gpu_layers": self._gpu.n_gpu_layers})
        return d
