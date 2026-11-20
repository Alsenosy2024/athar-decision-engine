"""Offline Chronos-Bolt worker. Executed in its own Python environment."""
import json
import socket
import sys


def serve(fd, checkpoint):
    import torch
    from chronos import BaseChronosPipeline

    torch.set_num_threads(2)
    model = BaseChronosPipeline.from_pretrained(
        checkpoint, device_map='cpu', torch_dtype=torch.float32, local_files_only=True,
    )
    with socket.socket(fileno=fd) as channel, channel.makefile('rb') as reader, torch.inference_mode():
        for line in reader:
            job = json.loads(line)
            inputs = [torch.tensor(series, dtype=torch.float32) for series in job['inputs']]
            quantiles, _ = model.predict_quantiles(
                inputs, prediction_length=job['horizon'], quantile_levels=[0.1, 0.5, 0.9],
            )
            rows = [row.T.tolist() if torch.isfinite(row).all() else None for row in quantiles]
            channel.sendall(json.dumps(rows, allow_nan=False).encode() + b'\n')


if __name__ == '__main__':
    serve(int(sys.argv[1]), sys.argv[2])
