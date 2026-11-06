"""Thin subprocess boundary to the pinned, unchanged AIG algorithms."""
import hashlib
import json
from pathlib import Path
import sys

CACHE = Path.home()/'Library/Caches/model-observatory-aig'
UPSTREAM = CACHE/'upstream'

def main():
    manifest = json.loads((Path(__file__).with_name('engine-hashes.json')).read_text(encoding='utf-8'))
    for filename, expected in manifest.items():
        if hashlib.sha256((UPSTREAM/filename).read_bytes()).hexdigest() != expected:
            raise RuntimeError('AIG source integrity mismatch')
    sys.path.insert(0,str(UPSTREAM))
    from services.api_checker.algorithms import common, bayes_score
    import numpy as np
    data=json.load(sys.stdin)
    if data['action']=='describe':
        result={'prompt':common.RANDOM_PROMPT,'min_samples':common.MIN_SAMPLES}
    else:
        samples=[common.extract_number(s) for s in data['texts']]
        if any(n is None for n in samples):
            raise ValueError('Invalid fingerprint output')
        if data['action']=='baseline':
            result=common.build_baseline(data['model'],data['model'],data['protocol'],samples)
        else:
            result=bayes_score.evaluate(samples,data['baselines'],claimed_name=data['model'],rng=np.random.default_rng(917))
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))

if __name__=='__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'error_type':type(error).__name__,'dependency':error.name if isinstance(error,ModuleNotFoundError) else ''}))
        sys.exit(1)
