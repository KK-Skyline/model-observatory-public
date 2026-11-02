"""Discover split/archived turn-context sources by embedded session identity."""
import json
import os
from pathlib import Path


def session_sources(home,wanted,report_error,gaps):
    result={}
    for folder in ('sessions','archived_sessions'):
        root=Path(home)/folder
        if not root.exists():continue
        def walk_error(error):report_error('selection_source_index_unreadable','session-index/'+folder,scope='evidence')
        for parent,directories,files in os.walk(root,followlinks=False,onerror=walk_error):
            for name in list(directories):
                if (Path(parent)/name).is_symlink():
                    directories.remove(name);gaps['session_index_symlinks_skipped']+=1
            for name in sorted(files):
                if not name.endswith('.jsonl'):continue
                path=Path(parent)/name
                source='session-index/'+str(path.relative_to(root))
                if path.is_symlink():
                    gaps['session_index_symlinks_skipped']+=1;continue
                try:
                    with path.open(encoding='utf-8') as stream:header=json.loads(next(stream))
                    if not isinstance(header,dict) or header.get('type')!='session_meta':raise ValueError('missing session header')
                    metadata=header.get('payload')
                    if not isinstance(metadata,dict) or not isinstance(metadata.get('id'),str) or not metadata['id']:raise ValueError('missing session identity')
                    if metadata['id'] in wanted:result.setdefault(metadata['id'],[]).append(path.resolve())
                except (OSError,ValueError,UnicodeError,StopIteration):
                    report_error('selection_source_index_invalid',source,scope='evidence')
    return result
