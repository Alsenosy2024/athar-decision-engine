"""Run Codex as a non-interactive, text-only JSON completion worker."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic

TIMEOUT = 90
_SLOT = Lock()


def find_codex():
    if binary := shutil.which('codex'):
        return Path(binary).absolute()
    home = Path.home()
    node_bins = sorted(home.glob('.nvm/versions/node/*/bin/codex'), reverse=True)
    for candidate in (*node_bins, home / '.bun/bin/codex', Path('/usr/local/bin/codex')):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def strict_schema(schema):
    """Make Pydantic's schema compatible with strict structured outputs."""
    if isinstance(schema, list):
        return [strict_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    if 'properties' in schema:
        schema = {**schema, 'required': list(schema['properties']), 'additionalProperties': False}
    return {('anyOf' if key == 'oneOf' else key): strict_schema(value)
            for key, value in schema.items() if key not in ('default', 'discriminator')}


def strict_object(content):
    def object_pairs(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError('Duplicate keys in Codex response')
        return result

    def invalid_constant(value):
        raise ValueError(f'Invalid JSON constant: {value}')

    result = json.loads(content, object_pairs_hook=object_pairs, parse_constant=invalid_constant)
    if not isinstance(result, dict):
        raise ValueError('Expected a JSON object')
    return result


def final_message(stdout):
    message = None
    complete = False
    for line in stdout.splitlines():
        event = json.loads(line)
        if event['type'] == 'item.completed' and event['item']['type'] == 'agent_message':
            message = event['item']['text']
        elif event['type'] == 'turn.completed':
            complete = True
    if not complete or not isinstance(message, str) or not message.strip() or len(message) > 20000:
        raise ValueError('Codex did not return a valid final message')
    return message.strip()


def _run(command, *, cwd, env, input, timeout):
    with subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, encoding='utf-8', start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            # The Node launcher can spawn the native binary. Reap the whole worker group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def complete(codex_home, prompt, schema, *, timeout=None):
    # Keep the queue bounded in time even if multiple HTTP requests arrive together.
    if not _SLOT.acquire(timeout=TIMEOUT if timeout is None else 0):
        raise TimeoutError('Codex worker queue timed out')
    try:
        return _execute(codex_home, prompt, schema, timeout=TIMEOUT if timeout is None else timeout)
    finally:
        _SLOT.release()


def _execute(codex_home, prompt, schema, *, timeout):
    binary = find_codex()
    if binary is None:
        raise FileNotFoundError('Codex CLI is unavailable')
    env = {key: os.environ[key] for key in ('HOME', 'USER', 'LOGNAME', 'TMPDIR', 'LANG', 'LC_ALL')
           if key in os.environ}
    env['PATH'] = os.pathsep.join((str(binary.parent), os.environ.get('PATH') or os.defpath))
    env['CODEX_HOME'] = str(Path(codex_home).expanduser().absolute())
    deadline = monotonic() + timeout
    with TemporaryDirectory(prefix='athar-codex-') as work:
        schema_path = Path(work) / 'response.json'
        schema_path.write_text(json.dumps(strict_schema(schema), ensure_ascii=False), encoding='utf-8')
        command = [str(binary), 'exec', '--skip-git-repo-check', '--json', '--ephemeral',
                   '--ignore-user-config', '--sandbox', 'read-only',
                   '-c', 'approval_policy="never"', '-c', 'web_search="disabled"',
                   '-c', 'project_doc_max_bytes=0', '--disable', 'shell_tool',
                   '--disable', 'apps', '--disable', 'plugins', '--disable', 'hooks',
                   '--disable', 'image_generation', '--disable', 'multi_agent_v2']

        def run(*options):
            return _run([*command, *options, '-'], cwd=work, env=env, input=prompt,
                        timeout=max(0, deadline - monotonic()))

        result = run('--output-schema', str(schema_path))
        if result.returncode and any(f"{word} argument '--output-schema'" in result.stderr
                                     for word in ('unexpected', 'unrecognized', 'unknown')):
            result = run()
        result.check_returncode()
        return final_message(result.stdout)
