"""Immutable resource generations with one atomic active reference."""
import json
import os
from pathlib import Path
import shutil
from tempfile import NamedTemporaryFile, mkdtemp
from uuid import uuid4


def atomic_json(path, value):
    path = Path(path)
    temporary = None
    try:
        with NamedTemporaryFile('w', dir=path.parent, prefix='.energy-state-', suffix='.part', encoding='utf-8', delete=False) as output:
            temporary = Path(output.name)
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def active_path(cache, resource_id):
    cache = Path(cache)
    pointer = cache / f'{resource_id}.active.json'
    try:
        data = json.loads(pointer.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return cache / f'{resource_id}.csv'
    relative = Path(data['generation'])
    parent = (cache / '.generations' / resource_id).resolve()
    folder = (cache / relative).resolve()
    if folder.parent != parent or data.get('version') != 1:
        raise ValueError('Referência de geração inválida.')
    path = folder / f'{resource_id}.csv'
    return path


def prepare(cache, resource_id):
    parent = Path(cache) / '.generations' / resource_id
    parent.mkdir(parents=True, exist_ok=True)
    folder = Path(mkdtemp(prefix='.pending-', dir=parent))
    return folder / f'{resource_id}.csv'


def publish(cache, resource_id, candidate, metadata):
    candidate = Path(candidate)
    generation = uuid4().hex
    metadata = metadata | {'generation': generation, 'schema_version': 1}
    atomic_json(candidate.with_suffix('.json'), metadata)
    for path in (candidate, candidate.with_suffix('.daily.sqlite')):
        with path.open('rb') as stream:
            os.fsync(stream.fileno())
    final = candidate.parent.parent / generation
    os.replace(candidate.parent, final)
    pointer = {'version': 1, 'generation': str(final.relative_to(cache))}
    atomic_json(Path(cache) / f'{resource_id}.active.json', pointer)
    return final / candidate.name


def cleanup_candidate(candidate):
    folder = Path(candidate).parent
    if folder.name.startswith('.pending-'):
        shutil.rmtree(folder, ignore_errors=True)


def recover(cache):
    """Only call while holding the application's update lock; retain completed generations."""
    root = Path(cache) / '.generations'
    if root.exists():
        for folder in root.glob('*/.pending-*'):
            if folder.is_dir() and not folder.is_symlink():
                shutil.rmtree(folder)
