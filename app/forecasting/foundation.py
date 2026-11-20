"""Persistent, offline TimesFM worker in the isolated Python environment."""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from threading import Lock
from time import monotonic

ENVIRONMENT = Path(__file__).resolve().parents[2] / '.venv-tsfm'
WORKER_SCRIPT = Path(__file__).resolve()
MAX_CONTEXT = 512


class TimesFMWorker:
    worker_script = None

    def __init__(self, environment=ENVIRONMENT, timeout=45):
        self.environment = environment
        self.timeout = timeout
        self._lock = Lock()
        self._process = self._socket = self._reader = None
        self._retry_at = 0.0

    def _start(self):
        python = self.environment / 'bin' / 'python'
        checkpoint = self.environment / 'checkpoint'
        if not python.is_file() or not (checkpoint / 'model.safetensors').is_file():
            return False

        self._socket, child = socket.socketpair()
        self._socket.settimeout(self.timeout)
        self._reader = self._socket.makefile('rb')
        with child:
            self._process = subprocess.Popen(
                [str(python), '-I', str(self.worker_script or WORKER_SCRIPT), str(child.fileno()), str(checkpoint)],
                pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**os.environ, 'HF_HUB_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1'},
            )
        return True

    def forecast(self, inputs, horizon):
        """Return one [P10, P50, P90] triple of arrays per input, or None."""
        if not self._lock.acquire(timeout=self.timeout):
            return None
        try:
            if monotonic() < self._retry_at:
                return None
            if self._process is None and not self._start():
                return None
            payload = {'inputs': [values[-MAX_CONTEXT:] for values in inputs], 'horizon': horizon}
            self._socket.sendall(json.dumps(payload, allow_nan=False).encode() + b'\n')
            result = json.loads(self._reader.readline())
            if not isinstance(result, list) or len(result) != len(inputs):
                raise ValueError('Invalid TimesFM response')
            return result
        except (OSError, ValueError):
            self._stop()
            self._retry_at = monotonic() + 30
            return None
        finally:
            self._lock.release()

    def _stop(self):
        if self._reader is not None:
            self._reader.close()
            self._socket.close()
        if self._process is not None:
            try:
                self._process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        self._process = self._socket = self._reader = None

    def close(self):
        with self._lock:
            self._stop()


def _serve(fd, checkpoint):
    import numpy as np
    import timesfm
    import torch

    torch.set_num_threads(4)
    model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
        checkpoint, local_files_only=True, torch_compile=False,
    )
    model.compile(timesfm.ForecastConfig(
        max_context=MAX_CONTEXT, max_horizon=4, per_core_batch_size=8,
        normalize_inputs=True, use_continuous_quantile_head=True,
        force_flip_invariance=True, infer_is_positive=True, fix_quantile_crossing=True,
    ))

    with socket.socket(fileno=fd) as channel, channel.makefile('rb') as reader, torch.inference_mode():
        for line in reader:
            job = json.loads(line)
            _, quantiles = model.forecast(
                horizon=job['horizon'], inputs=[np.asarray(values) for values in job['inputs']],
            )
            # Index 0 is the point forecast. Indices 1, 5 and 9 are the requested quantiles.
            bands = quantiles[:, :, [1, 5, 9]].transpose(0, 2, 1)
            rows = [row.tolist() if np.isfinite(row).all() else None for row in bands]
            channel.sendall(json.dumps(rows, allow_nan=False).encode() + b'\n')


if __name__ == '__main__':
    _serve(int(sys.argv[1]), sys.argv[2])
