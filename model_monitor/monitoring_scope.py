"""Classify explicit subagent sources; missing evidence stays in scope."""
import json


def is_subagent(source):
    if source is None:return False
    if isinstance(source,str):
        if source in ('subagent','guardian_review'):return True
        if not source.lstrip().startswith('{'):return False
        source=json.loads(source)
    if not isinstance(source,dict):raise ValueError('invalid thread source metadata')
    if 'subagent' not in source:return False
    if not isinstance(source['subagent'],dict):raise ValueError('invalid subagent source metadata')
    return True
