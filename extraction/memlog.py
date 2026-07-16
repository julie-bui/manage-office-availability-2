"""Temporary, deliberately loud process-memory logging for diagnosing a
real production issue: Crown Estate PDF (4.3MB) SIGKILLs the Render
free-tier worker for exceeding 512MB, but every local measurement of the
same file peaks around ~200MB — nowhere near that limit. Since local
testing hasn't reproduced the crash, this logs real RSS at key
checkpoints during processing so an actual Render run can show which
step is really consuming memory on the real server, instead of guessing
further from a local environment that clearly isn't behaving the same
way. Not meant to stay forever — pull it back out once the real culprit
is found from real logs.

Every call is a silent no-op on any error (missing psutil, whatever) —
this must never itself break a real request just to gather diagnostics.
Explicitly flushes stdout after each line: if the process gets SIGKILLed
moments after a checkpoint, an unflushed, buffered print would be lost
entirely, which would hide exactly the checkpoint we most need to see.
"""
_process = None


def log(checkpoint, filename=""):
    global _process
    try:
        import psutil

        if _process is None:
            _process = psutil.Process()
        rss_mb = _process.memory_info().rss / 1024 / 1024
        label = f" [{filename}]" if filename else ""
        print(f"[memlog]{label} {checkpoint}: RSS={rss_mb:.1f} MiB", flush=True)
    except Exception:
        pass
