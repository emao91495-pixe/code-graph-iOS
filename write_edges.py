import pickle, time, sys, os

# Add project root to sys.path
_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

from src.graph.store import Neo4jStore

print('Loading cache...', flush=True)
t0 = time.time()
with open('/tmp/cg_edges_cache.pkl', 'rb') as f:
    all_nodes, all_edges = pickle.load(f)
print(f'Loaded {len(all_edges)} edges in {time.time()-t0:.1f}s', flush=True)

store = Neo4jStore()
MAX_RETRY = 5
total = len(all_edges)
CHUNK = 10000

written = 0
t0 = time.time()
for start in range(0, total, CHUNK):
    chunk = all_edges[start:start+CHUNK]
    for attempt in range(MAX_RETRY):
        try:
            store.upsert_edges(chunk)
            written += len(chunk)
            break
        except Exception as e:
            wait = 2 ** attempt
            print(f'  retry {attempt+1}/{MAX_RETRY} at {start}: {type(e).__name__}, wait {wait}s', flush=True)
            time.sleep(wait)
            if attempt == MAX_RETRY - 1:
                print(f'FATAL at {start}: {e}', flush=True)
                store.close()
                sys.exit(1)
    pct = written / total * 100
    elapsed = time.time() - t0
    eta = elapsed / written * (total - written) if written else 0
    print(f'[{"#"*int(pct/5)}{"-"*(20-int(pct/5))}] {written}/{total} ({pct:.0f}%) ETA {eta:.0f}s', flush=True)

print(f'\nDone: {written} edges written in {time.time()-t0:.0f}s', flush=True)
store.close()
